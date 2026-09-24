import type { Language } from "../state/preferences";

/** Translate system diagnostics only, never manuscript or source content. */
export function diagnosticText(error: unknown, language: Language): string {
  const raw = error instanceof Error ? error.message : String(error || "");
  const code = typeof error === "object" && error ? String((error as { code?: string }).code || "") : "";
  const value = `${code} ${raw}`;
  const rules: [RegExp, string, string][] = [
    [/QUERY_TRANSLATION_FAILED/, "未能生成英文检索词，本次检索未开始。请重试，或使用英文主题和关键词。", "English search terms could not be prepared. Search has not started. Retry or use an English topic and keywords."],
    [/INSUFFICIENT_CREDIT|余额不足|额度不足|insufficient.credit/i, "余额不足，请补充余额后重试。", "Insufficient credit. Add credit and try again."],
    [/原论文图号尚未确认/, "原论文图号尚未确认，请在图像阶段核对来源，不能直接标记通过。", "The source figure number is unconfirmed. Check the source in Images before confirming."],
    [/图片来源记录与当前稿件不一致/, "图片来源记录与当前稿件不一致，请在图像阶段核对后重新同步。", "Figure source records differ from the manuscript. Check Images and sync again."],
    [/原图来源不可用/, "原图来源不可用，请在图像阶段补全来源或重新选择图片。", "The original image is unavailable. Restore its source or select another image in Images."],
    [/来源论文不可用/, "来源论文不可用，请先到文献库核对。", "The source paper is unavailable. Check it in Library first."],
    [/当前图片不可用/, "当前图片不可用，请在图像阶段检查。", "The current image is unavailable. Check it in Images."],
    [/无法唯一定位这张图/, "无法唯一定位这张图，请先同步终稿；仍无法定位时联系管理员。", "This figure cannot be uniquely located. Sync Final; contact an administrator if it persists."],
    [/图片、初稿或核对记录已变化/, "图片、初稿或核对记录已变化，请重新打开窗口。", "The image, Draft or review record changed. Reopen this window."],
    [/图注请使用纯文本/, "图注请使用纯文本，不要插入图片、HTML 或 Markdown 标记。", "Use plain text for the caption, without images, HTML or Markdown markup."],
    [/timeout|timed out|超时/i, "请求超时，请稍后重试。", "The request timed out. Try again shortly."],
    [/Failed to fetch|NetworkError|network error|网络连接/i, "网络连接失败，请检查网络后重试。", "The network request failed. Check your connection and retry."],
    [/unauthorized|not authenticated|未登录|登录已过期/i, "登录状态已失效，请重新登录。", "Your session has expired. Sign in again."],
    [/forbidden|permission denied|没有权限|无权访问/i, "没有权限执行此操作，请联系管理员。", "You do not have permission for this action. Contact an administrator."],
    [/selected bibliography candidate is unavailable or stale/i, "候选书目已经更新，请重新自动核验。", "The bibliography candidate changed. Run automatic verification again."],
    [/bibliography record is missing required fields/i, "候选书目信息不完整，现有结果已保留，请重新自动核验。", "The bibliography candidate is incomplete. Existing results were retained; run automatic verification again."],
    [/Manual bibliography resolution requires/i, "书目核验依据不完整，请补充来源位置后再保存。", "Bibliography evidence is incomplete. Add the source location before saving."],
    [/WORKFLOW_CONFLICT|STATE_CONFLICT|Workflow stage changed|stale|版本.*变化|changed.*(?:running|waiting)|revision/i, "内容或版本已更新，请刷新后重试。", "The content or version changed. Refresh and try again."],
    [/The outline could not be loaded/i, "无法载入该大纲，请检查内容后重试。", "The outline could not be loaded. Check its content and retry."],
    [/ARTIFACT_FILE_MISSING/, "文件暂时不可用，请刷新或联系管理员检查存储。", "The file is unavailable. Refresh or ask an administrator to check storage."],
    [/验证码发送失败|verification.*(?:delivery|send).*fail/i, "验证码发送失败，请稍后重试。", "The verification code could not be sent. Try again shortly."],
    [/invalid.*(?:password|credentials)|密码错误|账号或密码/i, "账号或密码不正确，请检查后重试。", "The account or password is incorrect. Check and try again."],
  ];
  const match = rules.find(([pattern]) => pattern.test(value));
  if (match) return match[language === "en" ? 2 : 1];
  const chinese = /[\u3400-\u9fff]/.test(raw);
  if (raw && raw.length <= 180 && (language === "en" ? !chinese : chinese)
      && !/Traceback|HTTP\s*\d|\{\s*"|Error:|Scientific task failed/i.test(raw)) return raw;
  // Unknown diagnostics remain available separately, not silently discarded.
  return language === "en" ? "The operation could not be completed. Check the details or contact an administrator."
    : "本次操作未完成，请查看详细原因，或联系管理员处理。";
}
