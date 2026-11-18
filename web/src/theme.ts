import { useLayoutEffect, useState, useSyncExternalStore } from 'react';

export type ThemePreference = 'system' | 'light' | 'dark';
// Keep the storage key in sync with the initial theme in index.html.
const themeKey = 'creator-radar-theme';
const systemTheme = matchMedia('(prefers-color-scheme: dark)');

function subscribeToSystemTheme(onChange: () => void) {
  systemTheme.addEventListener('change', onChange);
  return () => systemTheme.removeEventListener('change', onChange);
}

function readTheme(): ThemePreference {
  try {
    const saved = localStorage.getItem(themeKey);
    if (saved === 'light' || saved === 'dark') return saved;
  } catch {
    // The browser can block storage for this origin.
  }
  return 'system';
}

export function useTheme() {
  const [theme, setTheme] = useState(readTheme);
  const systemIsDark = useSyncExternalStore(subscribeToSystemTheme, () => systemTheme.matches);
  const resolvedTheme = theme === 'system' ? (systemIsDark ? 'dark' : 'light') : theme;

  useLayoutEffect(() => {
    document.documentElement.dataset.theme = resolvedTheme;
  }, [resolvedTheme]);

  function changeTheme(preference: ThemePreference) {
    setTheme(preference);
    try {
      localStorage.setItem(themeKey, preference);
    } catch {
      // The theme still applies for the current page.
    }
  }

  return { theme, resolvedTheme, changeTheme };
}
