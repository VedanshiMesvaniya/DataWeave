const themeMap = {
  dark: { value: 'dark', mode: 'dark' },
  light: { value: 'light', mode: 'light' },
  'academic-dark': { value: 'academic-dark', mode: 'dark' },
  'academic-light': { value: 'academic-light', mode: 'light' },
  'sonoct-light': { value: 'sonoct-light', mode: 'light' },
  'aurora-dark': { value: 'aurora-dark', mode: 'dark' },
}

export function resolveTheme(theme) {
  return themeMap[theme] || themeMap.dark
}

export function useResolvedTheme(theme) {
  return resolveTheme(theme)
}
