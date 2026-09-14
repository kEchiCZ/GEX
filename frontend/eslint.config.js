import js from '@eslint/js'
import reactHooks from 'eslint-plugin-react-hooks'
import reactRefresh from 'eslint-plugin-react-refresh'
import globals from 'globals'
import tseslint from 'typescript-eslint'

export default tseslint.config(
  { ignores: ['dist'] },
  {
    extends: [js.configs.recommended, ...tseslint.configs.recommended],
    files: ['**/*.{ts,tsx}'],
    languageOptions: {
      ecmaVersion: 2022,
      globals: globals.browser,
    },
    plugins: {
      'react-hooks': reactHooks,
      'react-refresh': reactRefresh,
    },
    rules: {
      // eslint-plugin-react-hooks 7: `recommended` = rules-of-hooks +
      // exhaustive-deps + pravidla React Compileru (#1123). Compiler v buildu
      // NEběží, takže jeho pravidla popisují idiomy, které by mu vadily, ne
      // chyby za běhu. Tři z nich hlásí na kódu 40 míst (stav 15. 9. 2026):
      // set-state-in-effect 22 (reset stavu při změně props), refs 15
      // (latest-ref / lazy init přes useRef), purity 3 (Date.now v renderu
      // bez tikajícího stavu) — všechno vědomé vzory, ne souběhy jako
      // v ListEditor (#1122). Drží se jako `warn`, aby byly vidět per místo
      // a nové výskyty nepřibývaly bez povšimnutí; přepnutí na `error` až po
      // průchodu po skupinách s vizuální kontrolou (druhá půlka #1123).
      // immutability a preserve-manual-memoization už nic nehlásí — zůstávají
      // jako `error` z recommended.
      ...reactHooks.configs.recommended.rules,
      'react-hooks/rules-of-hooks': 'error',
      'react-hooks/exhaustive-deps': 'warn',
      'react-hooks/set-state-in-effect': 'warn',
      'react-hooks/refs': 'warn',
      'react-hooks/purity': 'warn',
      'react-refresh/only-export-components': ['warn', { allowConstantExport: true }],
    },
  },
)
