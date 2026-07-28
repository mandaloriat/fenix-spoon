import { fileURLToPath } from 'node:url';

import { defineConfig } from 'vitest/config';

export default defineConfig({
  resolve: {
    alias: {
      // Resolve the sibling package from source so tests don't depend on build order.
      // Consumers still get the built `dist/` via the package's exports map.
      '@fenix-spoon/client': fileURLToPath(new URL('../client/src/index.ts', import.meta.url)),
    },
  },
  test: {
    environment: 'jsdom',
    setupFiles: ['tests/setup.ts'],
    include: ['tests/**/*.test.ts'],
  },
});
