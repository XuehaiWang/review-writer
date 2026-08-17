import { Component, type ErrorInfo, type ReactNode } from "react";
import { useQuery } from "@tanstack/react-query";
import { Navigate, Outlet, Route, Routes, useLocation } from "react-router-dom";

import { ApiError } from "../api/client";
import { authConfigQuery, meQuery } from "../api/queries";
import { AppShell } from "../components/AppShell";
import { ErrorState } from "../components/ErrorState";
import { LoadingView } from "../components/LoadingView";
import { AuthPage } from "../features/auth/AuthPage";
import { safeReturnPath } from "../features/auth/paths";
import { DiscoveryPage } from "../features/discovery/DiscoveryPage";
import { DraftPage } from "../features/draft/DraftPage";
import { FinalPage } from "../features/final/FinalPage";
import { LibraryPage } from "../features/library/LibraryPage";
import { LandingPage } from "../features/landing/LandingPage";
import { ImagesPage } from "../features/images/ImagesPage";
import { PlanningPage } from "../features/planning/PlanningPage";
import { ProjectsPage } from "../features/projects/ProjectsPage";
import { SectionsPage } from "../features/sections/SectionsPage";
import { SettingsPage } from "../features/settings/SettingsPage";
import { useUiText } from "../i18n/useUiText";

class ApplicationErrorBoundary extends Component<{ children: ReactNode }, { error: Error | null }> {
  state: { error: Error | null } = { error: null };

  static getDerivedStateFromError(error: Error) {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    console.error("Review Writer React application error", error, info.componentStack);
  }

  render() {
    if (this.state.error) {
      return (
        <main className="page-container workspace">
          <ErrorState
            error={this.state.error}
            onRetry={() => this.setState({ error: null })}
          />
        </main>
      );
    }
    return this.props.children;
  }
}

function AppRoutes() {
  const { text } = useUiText();
  const location = useLocation();
  const authConfig = useQuery(authConfigQuery);
  const identity = useQuery(meQuery);

  if (authConfig.isPending || identity.isPending) return <LoadingView />;
  if (authConfig.error) return <ErrorState title={text("无法读取服务配置", "Unable to read service configuration")} error={authConfig.error} onRetry={() => authConfig.refetch()} />;
  const signedOut = identity.error instanceof ApiError && identity.error.status === 401;
  if (identity.error && !signedOut) {
    return <ErrorState title={text("无法读取登录状态", "Unable to read sign-in status")} error={identity.error} onRetry={() => identity.refetch()} />;
  }
  const principal = signedOut ? null : identity.data || null;
  const requestedPath = `${location.pathname}${location.search}`;
  const loginTarget = `/login?next=${encodeURIComponent(requestedPath)}`;
  const postLoginTarget = safeReturnPath(new URLSearchParams(location.search).get("next"));

  return (
    <Routes>
      <Route path="/" element={<LandingPage authConfig={authConfig.data} identity={principal} />} />
      <Route path="/login" element={principal ? <Navigate to={postLoginTarget} replace /> : <AuthPage config={authConfig.data} />} />
      <Route
        element={principal ? (
          <AppShell authConfig={authConfig.data} identity={principal}><Outlet /></AppShell>
        ) : (
          <Navigate to={loginTarget} replace />
        )}
      >
        <Route path="/workspace" element={<ProjectsPage />} />
        <Route path="/library" element={<LibraryPage />} />
        <Route path="/discovery" element={<DiscoveryPage />} />
        <Route path="/planning" element={<PlanningPage />} />
        <Route path="/sections" element={<SectionsPage />} />
        <Route path="/images" element={<ImagesPage />} />
        <Route path="/draft" element={<DraftPage />} />
        <Route path="/final" element={<FinalPage />} />
        <Route path="/settings" element={<SettingsPage />} />
      </Route>
      <Route path="*" element={<Navigate to={principal ? "/workspace" : "/"} replace />} />
    </Routes>
  );
}

export function App() {
  return (
    <ApplicationErrorBoundary>
      <AppRoutes />
    </ApplicationErrorBoundary>
  );
}
