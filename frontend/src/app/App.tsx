import { Component, lazy, Suspense, type ErrorInfo, type ReactNode } from "react";
import { useQuery } from "@tanstack/react-query";
import { Navigate, Outlet, Route, Routes, useLocation } from "react-router-dom";

import { ApiError } from "../api/client";
import { authConfigQuery, meQuery } from "../api/queries";
import { AppShell } from "../components/AppShell";
import { ErrorState } from "../components/ErrorState";
import { LoadingView } from "../components/LoadingView";
import { safeReturnPath } from "../features/auth/paths";
import { useUiText } from "../i18n/useUiText";

const AdminPage = lazy(async () => ({
  default: (await import("../features/admin/AdminPage")).AdminPage,
}));
const AuthPage = lazy(async () => ({
  default: (await import("../features/auth/AuthPage")).AuthPage,
}));
const DiscoveryPage = lazy(async () => ({
  default: (await import("../features/discovery/DiscoveryPage")).DiscoveryPage,
}));
const DraftPage = lazy(async () => ({
  default: (await import("../features/draft/DraftPage")).DraftPage,
}));
const FinalPage = lazy(async () => ({
  default: (await import("../features/final/FinalPage")).FinalPage,
}));
const ImagesPage = lazy(async () => ({
  default: (await import("../features/images/ImagesPage")).ImagesPage,
}));
const LandingPage = lazy(async () => ({
  default: (await import("../features/landing/LandingPage")).LandingPage,
}));
const LibraryPage = lazy(async () => ({
  default: (await import("../features/library/LibraryPage")).LibraryPage,
}));
const PlanningPage = lazy(async () => ({
  default: (await import("../features/planning/PlanningPage")).PlanningPage,
}));
const ProjectsPage = lazy(async () => ({
  default: (await import("../features/projects/ProjectsPage")).ProjectsPage,
}));
const SectionsPage = lazy(async () => ({
  default: (await import("../features/sections/SectionsPage")).SectionsPage,
}));
const SettingsPage = lazy(async () => ({
  default: (await import("../features/settings/SettingsPage")).SettingsPage,
}));

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
    <Suspense fallback={<LoadingView />}>
      <Routes>
        <Route
          path="/"
          element={<LandingPage authConfig={authConfig.data} identity={principal} />}
        />
        <Route
          path="/login"
          element={
            principal ? (
              <Navigate to={postLoginTarget} replace />
            ) : (
              <AuthPage config={authConfig.data} />
            )
          }
        />
        <Route
          element={
            principal ? (
              <AppShell authConfig={authConfig.data} identity={principal}>
                <Outlet />
              </AppShell>
            ) : (
              <Navigate to={loginTarget} replace />
            )
          }
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
          <Route
            path="/admin"
            element={
              principal?.permissions.includes("provider:manage") ? (
                <AdminPage />
              ) : (
                <Navigate to="/settings" replace />
              )
            }
          />
        </Route>
        <Route
          path="*"
          element={<Navigate to={principal ? "/workspace" : "/"} replace />}
        />
      </Routes>
    </Suspense>
  );
}

export function App() {
  return (
    <ApplicationErrorBoundary>
      <AppRoutes />
    </ApplicationErrorBoundary>
  );
}
