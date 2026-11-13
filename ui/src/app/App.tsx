import { lazy, Suspense, useEffect, useRef } from 'react';
import { Route, Routes, useLocation, useNavigate } from 'react-router';
import { AnimatePresence, motion } from 'motion/react';
import { Overview } from '../features/overview/Overview';
import { replayControl } from './api';
import { Boundary } from './Boundary';
import { CommandPalette } from './CommandPalette';
import { LiveProvider } from './Live';
import { Shell, useApplyTheme } from './Shell';
import { useConsole } from './store';
import { useStream } from './stream';

const Decisions = lazy(() => import('../features/decisions/Decisions'));
const Markets = lazy(() => import('../features/markets/Markets'));
const Money = lazy(() => import('../features/money/Money'));
const Risk = lazy(() => import('../features/risk/Risk'));
const TradeStory = lazy(() => import('../features/trades/TradeStory'));
const Learning = lazy(() => import('../features/learning/Learning'));
const Rules = lazy(() => import('../features/rules/Rules'));
const Desk = lazy(() => import('../desk/DeskApp'));

// Prefetch every page shortly after load so switching pages never waits on a chunk.
const PREFETCH = [
  () => import('../features/decisions/Decisions'),
  () => import('../features/markets/Markets'),
  () => import('../features/money/Money'),
  () => import('../features/risk/Risk'),
  () => import('../features/learning/Learning'),
  () => import('../features/rules/Rules'),
  () => import('../desk/DeskApp'),
  () => import('../three/Core'),
];

const GO: Record<string, string> = { o: '/', d: '/decisions', m: '/markets', w: '/money', r: '/risk', l: '/learning', u: '/rules', k: '/desk' };

/** Space play/pause, N next event (replay); A activity; G then O/D/M/W/R/L/U/K to jump to a page. */
function useShortcuts() {
  const navigate = useNavigate();
  const gPressed = useRef(0);
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const el = e.target as HTMLElement | null;
      if (e.metaKey || e.ctrlKey || e.altKey) return;
      if (el && (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA' || el.tagName === 'SELECT' || el.isContentEditable)) return;
      const st = useConsole.getState();
      const scenario = st.source.startsWith('replay:') ? st.source.slice(7) : null;
      const key = e.key.toLowerCase();
      if (Date.now() - gPressed.current < 900 && GO[key]) {
        gPressed.current = 0;
        navigate(GO[key]);
        return;
      }
      if (key === 'g') {
        gPressed.current = Date.now();
      } else if (key === 'a') {
        st.setActivityOpen(!st.activityOpen);
      } else if (key === ' ' && scenario && el?.tagName !== 'BUTTON') {
        e.preventDefault();
        void replayControl(scenario, { action: st.status?.replay?.playing ? 'pause' : 'play' });
      } else if (key === 'n' && scenario) {
        void replayControl(scenario, { action: 'next' });
      }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [navigate]);
}

export function App() {
  useApplyTheme();
  useStream();
  useShortcuts();
  const location = useLocation();
  useEffect(() => {
    const t = setTimeout(() => PREFETCH.forEach((load) => void load()), 1500);
    return () => clearTimeout(t);
  }, []);
  return (
    <LiveProvider>
      <Shell>
        <AnimatePresence mode="wait" initial={false}>
          <motion.div
            key={location.pathname}
            initial={{ opacity: 0, y: 8 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0, y: -4 }}
            transition={{ duration: 0.22, ease: [0.22, 1, 0.36, 1] }}
          >
            <Boundary resetKey={location.pathname}>
              <Suspense fallback={null}>
                <Routes location={location}>
                  <Route path="/" element={<Overview />} />
                  <Route path="/decisions" element={<Decisions />} />
                  <Route path="/markets" element={<Markets />} />
                  <Route path="/markets/:symbol" element={<Markets />} />
                  <Route path="/money" element={<Money />} />
                  <Route path="/trades/:sleeve/:id" element={<TradeStory />} />
                  <Route path="/risk" element={<Risk />} />
                  <Route path="/learning" element={<Learning />} />
                  <Route path="/rules" element={<Rules />} />
                  <Route path="/desk" element={<Desk />} />
                  <Route path="*" element={<Overview />} />
                </Routes>
              </Suspense>
            </Boundary>
          </motion.div>
        </AnimatePresence>
        <CommandPalette />
      </Shell>
    </LiveProvider>
  );
}
