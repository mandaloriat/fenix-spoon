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
  type JobPage,
  type JobRequest,
  type JobResult,
  type JobStatus,
  type ObjectSummary,
  type ObjectType,
  type ObjectView,
  type ProtocolVersion,
  type SolverInfo,
  type StudyReport,
  type StudyRun,
  isTerminal,
} from './types.js';

/**
 * Split `geometry:g-12@3` into the parts the routes take as path segments.
 *
 * Here rather than in every caller, and *not* on the wire: the server rebuilds the canonical
 * reference from its own path parameters, which is what stops a URL whose halves disagree
 * from naming something unintended. A reference that is not one is a `FenixSpoonError`
 * rather than a request the server will reject a round trip later.
 *
 * Every method that calls this is `async`, deliberately, so a bad reference comes back as a
 * *rejected promise* rather than as a throw before any promise exists. `await` catches both —
 * an earlier version of this comment claimed otherwise and was wrong — but `.catch()`,
 * `Promise.all` and vitest's `rejects` only ever see the rejection, so a promise-returning
 * function that throws synchronously is a function with two error channels. Raised in review
 * of #21, after the same asymmetry had already made a test silently vacuous.
 */
function splitRef(ref: string): { type: string; id: string; revision?: number } {
  const match = /^([a-z_]+):([a-z]+-\d+)(?:@(\d+))?$/.exec(ref);
  if (!match) {
    // Status 0: this never reached the network, so borrowing a real HTTP code would be a
    // lie about where the refusal came from.
    throw new FenixSpoonError(`not an object reference: ${JSON.stringify(ref)}`, 0, undefined);
  }
  return {
    type: match[1]!,
    id: match[2]!,
    revision: match[3] === undefined ? undefined : Number(match[3]),
  };
}

/**
 * The study routes' URL for `study:s-3`, or `study:s-3@2` for a pinned one.
 *
 * Both halves of that are refusals the SDK can make without a round trip, and both were asked
 * for in review of #21. A reference of the wrong *type* — `geometry:g-1` — would otherwise
 * reach `/api/v1/studies/g-1/run` and come back a 422 from the server's own parser, which is
 * the right answer arriving later and from further away than necessary. And a revision must be
 * *carried*, not dropped: the routes take one as `?revision=`, so a client that parsed `@2` and
 * then ignored it would run the head while its caller believed it had pinned something.
 */
function studyPath(ref: string, suffix = ''): string {
  const { type, id, revision } = splitRef(ref);
  if (type !== 'study') {
    throw new FenixSpoonError(
      `not a study reference: ${JSON.stringify(ref)} names a ${type}`,
      0,
      undefined,
    );
  }
  const query = revision === undefined ? '' : `?revision=${revision}`;
  return `/api/v1/studies/${id}${suffix}${query}`;
}

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
  /**
   * API key for a server that requires one. Sent as `Authorization: Bearer` on HTTP
   * requests and as `?api_key=` on the event stream — a browser cannot put a header on
   * a WebSocket handshake, so the query string is the only way a page can authenticate
   * it. That places the key in a URL, where server logs may keep it: use per-user keys
   * you can revoke, not one shared secret.
   */
  apiKey?: string;
  /** Passed to every `fetch` — use it for extra headers or credentials. */
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
    // Browsers require `fetch` to be called with `window` as its receiver: a bare
    // reference throws "Illegal invocation", and calling it as `this.fetchImpl(…)`
    // would hand it this client instead. Node doesn't care, which is why this only
    // ever shows up in a real page. Bind whatever we selected — an injected
    // `window.fetch` is just as unbound as the global one, and re-binding an already
    // bound function or an arrow is a no-op.
    return impl.bind(globalThis);
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
    if (this.options.apiKey) absolute.searchParams.set('api_key', this.options.apiKey);
    return absolute.toString();
  }

  private async request<T>(path: string, init?: RequestInit): Promise<T> {
    // Merge through Headers rather than object spread: `fetchOptions.headers` may be a
    // Headers instance or an array of pairs, and spreading either silently drops the
    // caller's auth headers.
    const headers = new Headers(this.options.fetchOptions?.headers);
    new Headers(init?.headers).forEach((value, key) => headers.set(key, value));
    // Set last but only if absent, so an explicit Authorization in fetchOptions still wins.
    if (this.options.apiKey && !headers.has('Authorization')) {
      headers.set('Authorization', `Bearer ${this.options.apiKey}`);
    }

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
      // A string detail is the server explaining itself in prose ("job would use about
      // 4,194,304 cells, over this server's limit of…"). Put it in the message so a
      // demo printing `error.message` shows the explanation, not just the status code.
      // Structured details (pydantic's validation-error list) stay on `.detail` only.
      const explanation = typeof detail === 'string' && detail ? ` — ${detail}` : '';
      throw new FenixSpoonError(
        `${init?.method ?? 'GET'} ${path} failed: HTTP ${response.status}${explanation}`,
        response.status,
        detail,
      );
    }
    return (await response.json()) as T;
  }

  /**
   * What protocol the server speaks. The one endpoint that never requires a key, so it
   * works before you know whether your credential is right.
   *
   * ```ts
   * const { protocol } = await client.version();
   * const { compatible, reason } = checkProtocolCompatibility(protocol);
   * if (!compatible) throw new Error(reason);
   * ```
   */
  version(): Promise<ProtocolVersion> {
    return this.request<ProtocolVersion>('/api/v1/version');
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

  // ------------------------------------------------------------ workspace (1.10, 1.11)
  //
  // A reference is `geometry:g-12` and the routes take its two halves as path segments, so
  // these methods split it here rather than making every caller percent-encode a colon.

  /**
   * Create a workspace object and get back revision 1.
   *
   * ```ts
   * const geometry = await client.createObject('geometry', airfoil);
   * const design = await client.createObject('design', {
   *   solver: 'mock.laplace2d', geometry: geometry.ref, params: { resolution: 96 },
   * });
   * const job = await client.submit({ design: design.ref });
   * ```
   *
   * `body: object` rather than `Record<string, unknown>`, and the example above is why: an
   * *interface* — `Domain2D`, `Regions2D`, anything a caller declared — has no implicit index
   * signature in TypeScript, so passing a typed geometry to a `Record` parameter does not
   * compile. The first line of this doc comment was uncompilable when it was written. Caught
   * by the integration suite once it started creating geometries from the shared fixtures.
   */
  createObject(type: ObjectType, body: object, label?: string): Promise<ObjectView> {
    return this.request<ObjectView>(`/api/v1/objects/${type}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ body, label }),
    });
  }

  /** One object: the head, or the revision a pinned reference names. */
  async getObject(ref: string): Promise<ObjectView> {
    const { type, id, revision } = splitRef(ref);
    const suffix = revision === undefined ? '' : `?revision=${revision}`;
    return this.request<ObjectView>(`/api/v1/objects/${type}/${id}${suffix}`);
  }

  /**
   * Apply an RFC 6902 patch and get the next revision back.
   *
   * The revision it was computed from stays readable forever, which is what lets a result
   * name the exact inputs it came from long after the design has moved on.
   */
  async patchObject(
    ref: string,
    patch: Record<string, unknown>[],
    label?: string,
  ): Promise<ObjectView> {
    const { type, id } = splitRef(ref);
    return this.request<ObjectView>(`/api/v1/objects/${type}/${id}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ patch, label }),
    });
  }

  /** Which revisions of an object exist, ascending. */
  async objectRevisions(ref: string): Promise<number[]> {
    const { type, id } = splitRef(ref);
    const answer = await this.request<{ ref: string; revisions: number[] }>(
      `/api/v1/objects/${type}/${id}/revisions`,
    );
    return answer.revisions;
  }

  /** This principal's objects, newest first, without their bodies. */
  listObjects(type?: ObjectType): Promise<ObjectSummary[]> {
    const suffix = type ? `?type=${type}` : '';
    return this.request<ObjectSummary[]>(`/api/v1/objects${suffix}`);
  }

  /**
   * Submit every variation of a study. Resolves when the work is accepted, not when it is
   * done — `reused` says how much of it the result cache answered for free.
   *
   * Pass a pinned reference, `study:s-3@2`, to run the study as it was written then.
   */
  async runStudy(ref: string): Promise<StudyRun> {
    return this.request<StudyRun>(studyPath(ref, '/run'), { method: 'POST' });
  }

  /**
   * A study's table, and what it means. Safe to call before the run: there is no stored run
   * record to be absent, so an unrun study reports rows with nothing in them rather than a
   * 404 — which is what lets a page draw the shape before anything is pressed.
   */
  async studyReport(ref: string): Promise<StudyReport> {
    return this.request<StudyReport>(studyPath(ref));
  }

  /**
   * Job history, newest first. Spans restarts when the server has a persistent store.
   *
   * ```ts
   * const { jobs, total } = await client.listJobs({ limit: 20 });
   * ```
   */
  listJobs(options: { limit?: number; offset?: number } = {}): Promise<JobPage> {
    const query = new URLSearchParams();
    if (options.limit !== undefined) query.set('limit', String(options.limit));
    if (options.offset !== undefined) query.set('offset', String(options.offset));
    // `query.toString()` rather than `query.size`: the latter only reached Safari in 17.
    const encoded = query.toString();
    const suffix = encoded ? `?${encoded}` : '';
    return this.request<JobPage>(`/api/v1/jobs${suffix}`);
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
