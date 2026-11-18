import { useLayoutEffect, useState } from 'react';

export type ThemePreference = 'system' | 'light' | 'dark';
// Keep the storage key in sync with the initial theme in index.html.
const themeKey = 'creator-radar-theme';

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

  useLayoutEffect(() => {
    const root = document.documentElement;
    if (theme !== 'system') {
      root.dataset.theme = theme;
      return;
    }

    const system = matchMedia('(prefers-color-scheme: dark)');
    function followSystem() {
      root.dataset.theme = system.matches ? 'dark' : 'light';
    }
    followSystem();
    system.addEventListener('change', followSystem);
    return () => system.removeEventListener('change', followSystem);
  }, [theme]);

  function changeTheme(preference: ThemePreference) {
    setTheme(preference);
    try {
      localStorage.setItem(themeKey, preference);
    } catch {
      // The theme still applies for the current page.
    }
  }

  return { theme, changeTheme };
}
