import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import {
  BookOpenText, ChevronRight, Compass, History, LayoutDashboard, Menu,
  Radar, Search, Settings2, Users, Workflow, Zap,
} from 'lucide-react';
import { useState } from 'react';
import { createRoot } from 'react-dom/client';
import { BrowserRouter, Link, NavLink, Route, Routes, useLocation } from 'react-router-dom';
import { version } from '../package.json';
import { useApi, useLiveJobs, type Overview } from './api';
import { OverviewPage, QuickCapture, SettingsPage } from './pages';
import { AccountsPage, FeedPage, SearchPage, TopicsPage } from './discovery';
import { JobsPage, LogsPage } from './tasks';
import { useTheme } from './theme';
import { Empty } from './ui';
import './style.css';
import './workspace.css';

function Workspace() {
  const [menu, setMenu] = useState(false);
  const [quick, setQuick] = useState(false);
  const { theme, changeTheme } = useTheme();
  const { pathname } = useLocation();
  const navigation = [
    { path: '/', label: '总览', icon: LayoutDashboard, page: <OverviewPage /> },
    { path: '/feed', label: '情报流', icon: Radar, page: <FeedPage /> },
    { path: '/topics', label: '选题库', icon: BookOpenText, page: <TopicsPage /> },
    { path: '/accounts', label: '对标账号', icon: Users, page: <AccountsPage /> },
    { path: '/search', label: '关键词搜索', icon: Search, page: <SearchPage /> },
    { path: '/jobs', label: '采集任务', icon: Workflow, page: <JobsPage /> },
    { path: '/logs', label: '运行日志', icon: History, page: <LogsPage /> },
    { path: '/settings', label: '系统设置', icon: Settings2, page: <SettingsPage theme={theme} changeTheme={changeTheme} /> },
  ];
  useLiveJobs();
  const runningJobs = useApi<Overview>('overview', '', true, 60_000).data?.running_jobs || 0;
  return <div className="app-shell">
    <aside className={'sidebar' + (menu ? ' show' : '')}>
      <Link to="/" className="brand" onClick={() => setMenu(false)}><span className="brand-mark"><Radar size={24} /></span>
        <div><strong>Creator<span>Radar</span></strong><small>创作者工作台</small></div></Link>
      <nav aria-label="主导航">
        {[['工作台', navigation.slice(0, 5)], ['系统管理', navigation.slice(5)]].map(([label, links]) =>
          typeof links !== 'string' && <div className="nav-group" key={String(label)}><span>{String(label)}</span>
            {links.map(({ path, label, icon: Icon }) => <NavLink key={path} to={path} end onClick={() => setMenu(false)}>
              <Icon size={19} />{label}{path === '/jobs' && runningJobs > 0 && <b aria-hidden="true">{runningJobs}</b>}
            </NavLink>)}
          </div>)}
      </nav>
      <div className="sidebar-bottom"><span className="app-version">v{version}</span></div>
    </aside>
    {menu && <button className="mobile-backdrop" onClick={() => setMenu(false)} aria-label="关闭导航" />}
    <header className="topbar">
      <div className="breadcrumbs">
        <button className="icon-button mobile-menu" aria-label="打开导航" aria-expanded={menu}
          onClick={() => setMenu(!menu)}><Menu size={22} /></button>
        <Compass size={17} /><span>工作台</span><ChevronRight size={14} />
        <strong>{navigation.find(({ path }) => path === pathname)?.label || '页面未找到'}</strong>
      </div>
      <div className="top-actions"><button className="button primary quick-button" onClick={() => setQuick(true)}>
        <Zap size={17} fill="currentColor" />快速采集
      </button></div>
    </header>
    <main className="main">
      <Routes>
        {navigation.map(({ path, page }) => <Route key={path} path={path} element={page} />)}
        <Route path="*" element={<Empty title="页面未找到" />} />
      </Routes>
    </main>
    {quick && <QuickCapture close={() => setQuick(false)} />}
  </div>;
}

const client = new QueryClient({ defaultOptions: { queries: { retry: 1, staleTime: 10_000 } } });
createRoot(document.getElementById('root')!).render(
  <QueryClientProvider client={client}><BrowserRouter><Workspace /></BrowserRouter></QueryClientProvider>,
);
