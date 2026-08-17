import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useNavigate } from "react-router-dom";

import { apiRequest } from "../api/client";
import { queryKeys } from "../api/queries";

export function useSessionLogout() {
  const navigate = useNavigate();
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: () => apiRequest<void>("/api/v1/auth/logout", { method: "POST" }),
    onSuccess: async () => {
      await queryClient.cancelQueries();
      queryClient.removeQueries({
        predicate: (query) => query.queryKey[0] !== queryKeys.authConfig[0],
      });
      queryClient.setQueryData(queryKeys.me, null);
      navigate("/login", { replace: true });
    },
  });
}
