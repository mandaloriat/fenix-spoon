/**
 * `FenixSpoonClient` — the whole wire protocol behind a small typed surface.
 *
 * ```ts
 * const client = new FenixSpoonClient('http://localhost:8000');
 * const job = await client.submit({ solver: 'mock.laplace2d', geometry });
 * for await (const event of job.events()) {
 *   if (event.type === 'progress') console.log(event.iteration, event.residual);
 * }
 * const result = await job.result();
 * ```
 */

import {
  type ArtifactRef,
  type JobCreated,
  type JobEvent,
  type JobRequest,
  type JobResult,
  type JobStatus,
  type SolverInfo,
  isTerminal,
} from './types.js';

export class FenixSpoonError extends Error {
  readonly status: number;
  readonly detail: unknown;

  constructor(message: string, status: number, detail: unknown) {
    super(message);
    this.name = 'FenixSpoonError';
    this.status = status;
    this.detail = detail;
  }
}

/** Raised when a job ends in `failed` or `cancelled` while you were waiting on it. */
export class JobFailedError extends Error {
  readonly status: 'failed' | 'cancelled';

  constructor(status: 'failed' | 'cancelled', message: string) {
    super(message);
    this.name = 'JobFailedError';
    this.status = status;
  }
}

export interface ClientOptions {
  /** Passed to every `fetch` — use it for auth headers or credentials. */
  fetchOptions?: RequestInit;
  /** Injectable for tests / non-browser runtimes. Defaults to global `fetch`. */
  fetch?: typeof globalThis.fetch;
  /** Injectable WebSocket constructor. Defaults to global `WebSocket`. */
  WebSocket?: typeof globalThis.WebSocket;
  /**
   * How many times to reconnect a dropped event stream before giving up.
   * Reconnecting is cheap and safe: the server replays a job's whole event
   * history on connect, and the client drops the events it has already seen.
   */
  maxReconnects?: number;
  /** Base delay for reconnect backoff, in ms (doubles per attempt). */
  reconnectDelayMs?: number;
}

export interface SubscribeOptions {
  /** Abort the stream (and any pending reconnect). */
  signal?: AbortSignal;
}

export class FenixSpoonClient {
  readonly baseUrl: string;
  private readonly options: Required<Pick<ClientOptions, 'maxReconnects' | 'reconnectDelayMs'>> &
    ClientOptions;

  constructor(baseUrl = '', options: ClientOptions = {}) {
    this.baseUrl = baseUrl.replace(/\/+$/, '');
    this.options = { maxReconnects: 5, reconnectDelayMs: 250, ...options };
  }

  private get fetchImpl(): typeof globalThis.fetch {
    const impl = this.options.fetch ?? globalThis.fetch;
    if (!impl) throw new Error('no fetch implementation available; pass options.fetch');
    return impl;
  }

  private get socketImpl(): typeof globalThis.WebSocket {
    const impl = this.options.WebSocket ?? globalThis.WebSocket;
    if (!impl) throw new Error('no WebSocket implementation available; pass options.WebSocket');
    return impl;
  }

  /** Absolute URL for a protocol path, e.g. an {@link ArtifactRef.url}. */
  url(path: string): string {
    return `${this.baseUrl}${path.startsWith('/') ? path : `/${path}`}`;
  }

  private wsUrl(path: string): string {
    const absolute = new URL(this.url(path), globalThis.location?.href ?? 'http://localhost');
    absolute.protocol = absolute.protocol === 'https:' ? 'wss:' : 'ws:';
    return absolute.toString();
  }

  private async request<T>(path: string, init?: RequestInit): Promise<T> {
    // Merge through Headers rather than object spread: `fetchOptions.headers` may be a
    // Headers instance or an array of pairs, and spreading either silently drops the
    // caller's auth headers.
    const headers = new Headers(this.options.fetchOptions?.headers);
    new Headers(init?.headers).forEach((value, key) => headers.set(key, value));

    const response = await this.fetchImpl(this.url(path), {
      ...this.options.fetchOptions,
      ...init,
      headers,
    });
    if (!response.ok) {
      let detail: unknown;
      try {
        detail = (await response.json())?.detail;
      } catch {
        detail = await response.text().catch(() => undefined);
      }
      throw new FenixSpoonError(
        `${init?.method ?? 'GET'} ${path} failed: HTTP ${response.status}`,
        response.status,
        detail,
      );
    }
    return (await response.json()) as T;
  }

  /** Solvers installed on this server, with a JSON Schema for each one's params. */
  listSolvers(): Promise<SolverInfo[]> {
    return this.request<SolverInfo[]>('/api/v1/solvers');
  }

  /** Submit a job. Resolves as soon as the server accepts it, not when it finishes. */
  async submit(request: JobRequest): Promise<Job> {
    const created = await this.request<JobCreated>('/api/v1/jobs', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(request),
    });
    return new Job(this, created.job_id);
  }

  /** Attach to a job submitted elsewhere (a saved id, another tab). */
  job(jobId: string): Job {
    return new Job(this, jobId);
  }

  status(jobId: string): Promise<JobStatus> {
    return this.request<JobStatus>(`/api/v1/jobs/${jobId}`);
  }

  result(jobId: string): Promise<JobResult> {
    return this.request<JobResult>(`/api/v1/jobs/${jobId}/result`);
  }

  cancel(jobId: string): Promise<JobStatus> {
    return this.request<JobStatus>(`/api/v1/jobs/${jobId}/cancel`, { method: 'POST' });
  }

  artifactUrl(artifact: ArtifactRef): string {
    return this.url(artifact.url);
  }

  /**
   * Stream a job's events, oldest first, ending after the terminal status event.
   *
   * The server replays a job's full history on connect, so subscribing late still
   * yields every event — and a dropped connection can simply be re-established.
   * This iterator does that transparently, skipping events already delivered.
   */
  async *events(jobId: string, options: SubscribeOptions = {}): AsyncGenerator<JobEvent> {
    let delivered = 0;
    let attempt = 0;

    while (true) {
      let seen = 0;
      let finished = false;
      try {
        for await (const event of this.streamOnce(jobId, options.signal)) {
          seen += 1;
          if (seen <= delivered) continue; // replayed event we already handed out
          delivered = seen;
          yield event;
          if (event.type === 'status' && isTerminal(event.status)) {
            finished = true;
            return;
          }
        }
      } catch (error) {
        if (options.signal?.aborted) throw error;
        if (attempt >= this.options.maxReconnects) throw error;
      }
      if (finished || options.signal?.aborted) return;

      // The socket closed without a terminal event: the job is probably still
      // running and something dropped the connection. Back off and reattach.
      if (attempt >= this.options.maxReconnects) {
        throw new Error(
          `event stream for ${jobId} closed without a terminal event after ` +
            `${attempt} reconnection attempts`,
        );
      }
      await delay(this.options.reconnectDelayMs * 2 ** attempt, options.signal);
      attempt += 1;
    }
  }

  /** One WebSocket lifetime: yields events until the socket closes or errors. */
  private streamOnce(jobId: string, signal?: AbortSignal): AsyncGenerator<JobEvent> {
    const socket = new this.socketImpl(this.wsUrl(`/api/v1/jobs/${jobId}/events`));
    const queue: JobEvent[] = [];
    let notify: (() => void) | undefined;
    let closed = false;
    let failure: Error | undefined;

    const wake = () => {
      notify?.();
      notify = undefined;
    };
    socket.onmessage = (message: MessageEvent) => {
      try {
        queue.push(JSON.parse(String(message.data)) as JobEvent);
      } catch (error) {
        failure = error instanceof Error ? error : new Error(String(error));
        closed = true;
      }
      wake();
    };
    socket.onerror = () => {
      failure ??= new Error(`event stream for ${jobId} errored`);
      closed = true;
      wake();
    };
    socket.onclose = () => {
      closed = true;
      wake();
    };
    const onAbort = () => {
      failure ??= new DOMException('aborted', 'AbortError');
      closed = true;
      try {
        socket.close();
      } catch {
        /* already closing */
      }
      wake();
    };
    signal?.addEventListener('abort', onAbort, { once: true });

    async function* drain(): AsyncGenerator<JobEvent> {
      try {
        while (true) {
          while (queue.length) yield queue.shift()!;
          if (closed) {
            if (failure) throw failure;
            return;
          }
          await new Promise<void>((resolve) => {
            notify = resolve;
          });
        }
      } finally {
        signal?.removeEventListener('abort', onAbort);
        try {
          socket.close();
        } catch {
          /* already closed */
        }
      }
    }

    return drain();
  }
}

/** A handle to one submitted job. */
export class Job {
  constructor(
    readonly client: FenixSpoonClient,
    readonly id: string,
  ) {}

  status(): Promise<JobStatus> {
    return this.client.status(this.id);
  }

  result(): Promise<JobResult> {
    return this.client.result(this.id);
  }

  cancel(): Promise<JobStatus> {
    return this.client.cancel(this.id);
  }

  events(options?: SubscribeOptions): AsyncGenerator<JobEvent> {
    return this.client.events(this.id, options);
  }

  /**
   * Consume the event stream to completion, then fetch the result.
   *
   * `onEvent` receives *every* event, progress and status alike — status transitions
   * are worth surfacing too ("meshing", "solving"). Narrow with `event.type` if you
   * only care about progress. Throws {@link JobFailedError} if the job fails or is
   * cancelled.
   */
  async wait(
    onEvent?: (event: JobEvent) => void,
    options?: SubscribeOptions,
  ): Promise<JobResult> {
    for await (const event of this.events(options)) {
      onEvent?.(event);
      if (event.type === 'status' && (event.status === 'failed' || event.status === 'cancelled')) {
        throw new JobFailedError(event.status, event.error ?? `job ${event.status}`);
      }
    }
    return this.result();
  }
}

function delay(ms: number, signal?: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    if (signal?.aborted) {
      reject(new DOMException('aborted', 'AbortError'));
      return;
    }
    const timer = setTimeout(() => {
      signal?.removeEventListener('abort', onAbort);
      resolve();
    }, ms);
    const onAbort = () => {
      clearTimeout(timer);
      reject(new DOMException('aborted', 'AbortError'));
    };
    signal?.addEventListener('abort', onAbort, { once: true });
  });
}
