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

import {
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
