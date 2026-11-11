import React, { Component, type ErrorInfo, type ReactNode } from 'react';
import { createRoot } from 'react-dom/client';
import App from './App';

class ApplicationBoundary extends Component<{ children: ReactNode }, { failed: boolean }> {
  state = { failed: false };
  static getDerivedStateFromError() { return { failed: true }; }
  componentDidCatch(_error: Error, _info: ErrorInfo) { /* Deliberately do not expose customer content or exception diagnostics. */ }
  render() {
    if (this.state.failed) return <div className="empty-chat" style={{ height: '100vh' }}><h2>工作台界面暂时无法显示</h2><p>请重新打开窗口。已经持久化的草稿由接待服务保管。</p><button className="button" type="button" onClick={() => window.location.reload()}>重新载入界面</button></div>;
    return this.props.children;
  }
}
const root = document.getElementById('root');
if (!root) throw new Error('Missing renderer root');
createRoot(root).render(<ApplicationBoundary><App /></ApplicationBoundary>);
