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
      // chyby za běhu. Po průchodu 15. 9. 2026 jsou set-state-in-effect, refs
      // a purity `error`: reset stavu při změně klíče se řeší stavem, který
      // klíč nese, a odvozením při renderu; latest-ref přes useLayoutEffect;
      // Date.now v tikajícím stavu. Pět vědomých výjimek má
      // `eslint-disable-next-line` s důvodem na místě (reset per klíč
      // v useDayData, schránka deníku, klouzavé okno #487, jednorázový fit
      // pohledu v Heatmap, západka přepojení v SettingsView).
      ...reactHooks.configs.recommended.rules,
      'react-hooks/rules-of-hooks': 'error',
      'react-hooks/exhaustive-deps': 'warn',
      'react-hooks/set-state-in-effect': 'error',
      'react-hooks/refs': 'error',
      'react-hooks/purity': 'error',
      'react-refresh/only-export-components': ['warn', { allowConstantExport: true }],
    },
  },
)
