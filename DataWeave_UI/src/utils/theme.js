const themeMap = {
  'academic-dark': { value: 'academic-dark', mode: 'dark' },
  'academic-light': { value: 'academic-light', mode: 'light' },
}

export function resolveTheme(theme) {
  return themeMap[theme] || themeMap['academic-dark']
}

export function useResolvedTheme(theme) {
  return resolveTheme(theme)
}
