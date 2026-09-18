import "@testing-library/jest-dom/vitest";
// jsdom does not implement native dialog focus/top-layer methods.
HTMLDialogElement.prototype.showModal = function () { this.setAttribute("open", ""); };
HTMLDialogElement.prototype.close = function () { this.removeAttribute("open"); };
