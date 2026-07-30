/**
 * The JS half of the protocol conformance suite (#11).
 *
 * These read the *same* fixture files that `server/tests/test_protocol_fixtures.py`
 * reads. Every payload the server accepts, the SDK must accept; every payload the
 * server rejects, the SDK must reject. If one side's rules drift, this goes red.
 */

import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

import { describe, expect, it } from 'vitest';

import { PROTOCOL_VERSION, checkProtocolCompatibility } from '../src/types.js';
import {
  SERIES_LIMITS,
  validateGeometry,
  validateJobEvent,
  validateJobRequest,
  validateJobResult,
} from '../src/validate.js';

const FIXTURES = join(dirname(fileURLToPath(import.meta.url)), '../../../../protocol/fixtures');

interface Case {
  name: string;
  reason?: string;
  payload: unknown;
}

interface Corpus {
  valid: Case[];
  invalid: Case[];
}

function corpus(file: string): Corpus {
  return JSON.parse(readFileSync(join(FIXTURES, file), 'utf8')) as Corpus;
}

const SUITES: [string, (value: unknown) => unknown][] = [
  ['geometries.json', validateGeometry],
  ['events.json', validateJobEvent],
  ['results.json', validateJobResult],
  ['job-requests.json', validateJobRequest],
];

for (const [file, validate] of SUITES) {
  describe(file, () => {
    const { valid, invalid } = corpus(file);

    it('has fixtures to check', () => {
      expect(valid.length).toBeGreaterThan(0);
      expect(invalid.length).toBeGreaterThan(0);
    });

    for (const testCase of valid) {
      it(`accepts: ${testCase.name}`, () => {
        expect(() => validate(testCase.payload)).not.toThrow();
      });
    }

    for (const testCase of invalid) {
      it(`rejects: ${testCase.name}`, () => {
        expect(() => validate(testCase.payload)).toThrow();
      });
    }
  });
}

describe('results.json series ceilings', () => {
  const { series_limits: block } = JSON.parse(
    readFileSync(join(FIXTURES, 'results.json'), 'utf8'),
  ) as { series_limits: Record<string, number | string> };
  // `$comment` is prose, as everywhere else in the corpus; the rest is the contract.
  const declared = Object.fromEntries(
    Object.entries(block).filter(([key]) => key !== '$comment'),
  );

  it('are the ones this validator enforces', () => {
    // The same tripwire version.json provides for PROTOCOL_VERSION. `validate.ts` promises
    // "where the server rejects, these reject", and it can only keep that promise if the two
    // sets of ceilings are pinned to one place — the Python suite asserts these same four.
    expect(declared).toEqual({ ...SERIES_LIMITS });
  });
});

describe('version.json', () => {
  const { protocol_version: declared } = JSON.parse(
    readFileSync(join(FIXTURES, 'version.json'), 'utf8'),
  ) as { protocol_version: string };

  it('is the version this SDK is written against', () => {
    // The other half of the tripwire: the Python suite asserts this same corpus value
    // against the server's PROTOCOL_VERSION. Bumping one side alone fails on the other.
    expect(declared).toBe(PROTOCOL_VERSION);
  });

  it('accepts an older server on the same major, and says so', () => {
    const verdict = checkProtocolCompatibility('1.0', '1.4');
    expect(verdict.compatible).toBe(true);
    expect(verdict.reason).toMatch(/older/);
  });

  it('accepts a newer server on the same major — additive by definition', () => {
    expect(checkProtocolCompatibility('1.9', '1.2')).toMatchObject({ compatible: true });
  });

  it('refuses a different major', () => {
    const verdict = checkProtocolCompatibility('2.0', '1.0');
    expect(verdict.compatible).toBe(false);
    expect(verdict.reason).toMatch(/breaking/);
  });

  it('treats 1.10 as later than 1.9, not earlier', () => {
    // The reason `protocol` is a string on the wire: as a float, 1.10 parses to 1.1.
    expect(checkProtocolCompatibility('1.10', '1.9').reason).toMatch(/newer/);
  });

  it('refuses something it cannot parse rather than guessing', () => {
    expect(checkProtocolCompatibility('banana').compatible).toBe(false);
  });

  it.each(['1.banana', '1', '1.2.3', '', 'v1.0'])(
    'refuses the half-parseable version %o instead of defaulting the minor',
    (version) => {
      // The regression: defaulting a missing or unparseable minor to 0 made '1.banana'
      // compare equal to '1.0', so a server announcing nonsense read as fully compatible.
      const verdict = checkProtocolCompatibility(version, '1.0');
      expect(verdict.compatible).toBe(false);
      expect(verdict.reason).toMatch(/MAJOR\.MINOR/);
    },
  );
});
