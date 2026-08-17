import { create } from "zustand";
import { persist } from "zustand/middleware";

export type Language = "zh-CN" | "en";

type PreferenceState = {
  language: Language;
  setLanguage: (language: Language) => void;
};

export const usePreferences = create<PreferenceState>()(
  persist(
    (set) => ({
      language: "zh-CN",
      setLanguage: (language) => set({ language }),
    }),
    {
      name: "review-writer-preferences",
      partialize: ({ language }) => ({ language }),
    },
  ),
);
