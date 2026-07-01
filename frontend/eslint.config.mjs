import next from 'eslint-config-next';

const eslintConfig = [
  {
    ignores: [
      'node_modules/**',
      '.next/**',
      'out/**',
      'build/**',
      'next-env.d.ts',
    ],
  },
  ...next,
  {
    rules: {
      '@next/next/no-html-link-for-pages': 'off',
      'react/no-unescaped-entities': 'off',
      // react-hooks/immutability and react-hooks/set-state-in-effect are new
      // experimental rules in eslint-plugin-react-hooks v5 (pulled in by
      // eslint-config-next 16). They were absent in v14 (eslint 8) and flag
      // widespread, legitimate patterns: stable self-referential callbacks for
      // WebSocket reconnect, and setState-in-effect for derived form state.
      // Tracked as Phase 5 P2 tech debt for a dedicated refactor. All mature
      // react-hooks rules (rules-of-hooks, exhaustive-deps) remain active.
      'react-hooks/immutability': 'off',
      'react-hooks/set-state-in-effect': 'off',
      'react-hooks/purity': 'off',
    },
  },
];

export default eslintConfig;
