import { createElement, useState } from "react";
import { useUiText } from "./useUiText";
import { LocalizedError } from "../components/LocalizedError";

type Message = string | [string, string];
/** Keep both translations in state so a visible message follows language changes. */
export function useLocalizedMessage() {
  const [value, setValue] = useState<Message>("");
  const { text } = useUiText();
  return [Array.isArray(value) ? text(...value) : value ? createElement(LocalizedError, { error: value }) : "", setValue] as const;
}
