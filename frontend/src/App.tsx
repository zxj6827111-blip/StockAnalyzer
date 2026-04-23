import {
  Activity,
  Brain,
  Briefcase,
  Eye,
  LayoutDashboard,
  Newspaper,
  Settings,
} from 'lucide-react';
import { useEffect } from 'react';
import { BrowserRouter, NavLink, Route, Routes, useLocation } from 'react-router-dom';

import { prefetchTrainingOverview } from './lib/trainingOverviewCache';
import DashboardPage from './pages/Dashboard';
import LearningOverviewPage from './pages/LearningOverview';
import NewsPage from './pages/News';
import ObservationPoolPage from './pages/ObservationPoolPage';
import PortfolioPage from './pages/Portfolio';
import RuntimeStagePage from './pages/RuntimeStage';
import SystemOpsPage from './pages/SystemOps';

function Sidebar(props: { onWarmLearningOverview: () => void }) {
  const location = useLocation();
  const navItems = [
    { path: '/', label: '控制大屏', icon: LayoutDashboard },
    { path: '/runtime-stage', label: '系统阶段', icon: Activity },
    { path: '/learning-overview', label: '训练总览', icon: Brain },
    { path: '/observation-pool', label: '观察池', icon: Eye },
    { path: '/portfolio', label: '持仓与实盘', icon: Briefcase },
    { path: '/news', label: '新闻与因子', icon: Newspaper },
    { path: '/ops', label: '系统与日志', icon: Settings },
  ];

  return (
    <aside className="fixed left-0 top-0 flex h-screen w-64 flex-col border-r border-panelBorder bg-[rgba(10,25,37,0.7)] backdrop-blur-md">
      <div className="flex items-center gap-3 border-b border-panelBorder p-6">
        <div className="flex h-8 w-8 items-center justify-center rounded-lg bg-gradient-to-tr from-accent to-blue-500 shadow-[0_0_15px_rgba(65,214,179,0.4)]">
          <Activity className="h-5 w-5 text-bg" />
        </div>
        <h1 className="font-mono text-lg font-bold tracking-wider text-ink">StockAnalyzer</h1>
      </div>

      <nav className="flex flex-1 flex-col gap-2 p-4">
        {navItems.map(({ path, label, icon: Icon }) => {
          const isActive = location.pathname === path;
          const shouldWarmLearningOverview = path === '/learning-overview';
          return (
            <NavLink
              key={path}
              to={path}
              onMouseEnter={shouldWarmLearningOverview ? props.onWarmLearningOverview : undefined}
              onFocus={shouldWarmLearningOverview ? props.onWarmLearningOverview : undefined}
              className={`flex items-center gap-3 rounded-xl border px-4 py-3 font-medium transition-all ${
                isActive
                  ? 'border-[rgba(65,214,179,0.3)] bg-[rgba(65,214,179,0.1)] text-accent shadow-[0_0_20px_rgba(65,214,179,0.1)]'
                  : 'border-transparent text-muted hover:bg-[rgba(94,136,170,0.1)] hover:text-ink'
              }`}
            >
              <Icon className={`h-5 w-5 ${isActive ? 'text-accent' : 'text-muted'}`} />
              {label}
            </NavLink>
          );
        })}
      </nav>

      <div className="mx-4 mb-4 mt-auto border-t border-panelBorder p-4">
        <div className="flex items-center gap-2 font-mono text-xs text-muted">
          <div className="h-2 w-2 animate-pulse rounded-full bg-good shadow-[0_0_8px_#4ddf7e]" />
          本地 docker 运行视图
        </div>
      </div>
    </aside>
  );
}

function MainLayout() {
  const warmLearningOverview = () => {
    void prefetchTrainingOverview().catch(() => undefined);
  };

  useEffect(() => {
    const runtimeGlobal = globalThis as typeof globalThis & {
      window?: Window;
      requestIdleCallback?: (
        callback: IdleRequestCallback,
        options?: IdleRequestOptions,
      ) => number;
      cancelIdleCallback?: (handle: number) => void;
    };
    if (typeof runtimeGlobal.window === 'undefined') {
      return undefined;
    }
    if (
      typeof runtimeGlobal.requestIdleCallback === 'function' &&
      typeof runtimeGlobal.cancelIdleCallback === 'function'
    ) {
      const handle = runtimeGlobal.requestIdleCallback(
        () => {
          warmLearningOverview();
        },
        { timeout: 1500 },
      );
      return () => runtimeGlobal.cancelIdleCallback(handle);
    }
    const timer = globalThis.setTimeout(() => {
      warmLearningOverview();
    }, 800);
    return () => globalThis.clearTimeout(timer);
  }, []);

  return (
    <div className="flex h-screen overflow-hidden">
      <Sidebar onWarmLearningOverview={warmLearningOverview} />
      <main className="ml-64 h-screen flex-1 overflow-x-hidden overflow-y-auto p-8">
        <Routes>
          <Route path="/" element={<DashboardPage />} />
          <Route path="/runtime-stage" element={<RuntimeStagePage />} />
          <Route path="/learning-overview" element={<LearningOverviewPage />} />
          <Route path="/observation-pool" element={<ObservationPoolPage />} />
          <Route path="/portfolio" element={<PortfolioPage />} />
          <Route path="/news" element={<NewsPage />} />
          <Route path="/ops" element={<SystemOpsPage />} />
        </Routes>
      </main>
    </div>
  );
}

export default function App() {
  const basename = (import.meta.env.BASE_URL || '/').replace(/\/$/, '') || '/';
  return (
    <BrowserRouter basename={basename}>
      <MainLayout />
    </BrowserRouter>
  );
}
