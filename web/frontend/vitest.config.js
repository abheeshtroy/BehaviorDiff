import { defineConfig } from "vitest/config";

// Deliberately standalone rather than extending vite.config.js: these tests
// cover pure helpers, so they need neither the react plugin nor a DOM.
export default defineConfig({
  test: {
    environment: "node",
    include: ["src/lib/**/*.test.js"],
  },
});
