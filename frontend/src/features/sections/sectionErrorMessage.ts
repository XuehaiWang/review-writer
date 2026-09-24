import type { UiText } from "./sectionStatusLabels";

/** User-facing summary only; task records retain the original diagnostic. */
export function sectionErrorMessage(message: string, text: UiText): string {
  if (/conclusion deferred/i.test(message)) return text("等待正文完成后生成总结。", "The conclusion will follow the unfinished body sections.");
  if (/source checking is incomplete|source_check_incomplete/i.test(message)) return text("该章部分论断尚未通过原文核对；已保存中间结果，可检查证据后继续生成。", "Some claims in this section have not passed source checking. The intermediate work is saved; review the evidence before resuming.");
  if (/quota|insufficient.credit|额度|余额/i.test(message)) return text("模型服务额度不足，请联系管理员处理后继续。", "Model service credit is insufficient. Contact the administrator before continuing.");
  if (/invalid.api.key|authentication|授权不可用/i.test(message)) return text("模型服务授权异常，请联系管理员处理。", "Model service authorization failed. Contact the administrator.");
  if (/timeout|timed out|524|响应超时/i.test(message)) return text("模型服务响应超时，请稍后继续生成。", "The model service timed out. Resume generation shortly.");
  if (/provider.*unavailable|模型服务暂时不可用/i.test(message)) return text("模型服务暂时不可用，请稍后继续生成。", "The model service is temporarily unavailable. Resume generation shortly.");
  if (/Workflow stage changed/i.test(message)) return text("页面状态已更新，请刷新后重试。", "The workflow changed. Refresh the page and try again.");
  if (message.length <= 120 && /[\u4e00-\u9fff]/.test(message) && !/Scientific|Traceback|Error:/.test(message)) return text(message, "The operation did not finish. Retry, or contact the administrator if it fails again.");
  return text("本次操作未完成，请重试；若仍失败，请联系管理员。", "The operation did not finish. Retry, or contact the administrator if it fails again.");
}
