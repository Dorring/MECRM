module.exports = {
  preset: 'ts-jest',
  testEnvironment: 'node',
  setupFiles: ['<rootDir>/src/jest.setup.ts'],
  testMatch: [
    '<rootDir>/src/tests/**/*.test.ts',
    '**/tests/test_rls_enforcement.ts',
  ],
  testPathIgnorePatterns: ['<rootDir>/dist/', '<rootDir>/src/tests/helpers/'],
  maxWorkers: 1,
  collectCoverage: true,
  coverageDirectory: 'coverage',
  coverageThreshold: {
    global: {
      statements: 0,
      branches: 0,
      functions: 0,
      lines: 0,
    },
  },
};
