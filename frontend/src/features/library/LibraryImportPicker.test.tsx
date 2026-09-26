import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { LibraryImportPicker } from "./LibraryImportPicker";

afterEach(cleanup);

describe("library import selection", () => {
  it("filters folders recursively, preserves paths and permits deselection", () => {
    const submit = vi.fn();
    const { container } = render(<LibraryImportPicker busy={false} onFiles={submit} onArchive={vi.fn()} />);
    container.querySelector("details")!.open = true;
    const files = ["paper.pdf", "paper.pdf", "metadata.json"].map(name => new File(["x"], name));
    Object.defineProperty(files[0], "webkitRelativePath", { value: "one/paper.pdf" });
    Object.defineProperty(files[1], "webkitRelativePath", { value: "two/paper.pdf" });
    fireEvent.change(screen.getByLabelText("选择文件夹"), { target: { files } });
    expect(screen.getByText(/已选 2 个 PDF/)).toBeInTheDocument();
    expect(screen.getByText(/忽略 1 个其他文件/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("checkbox", { name: "two/paper.pdf" }));
    fireEvent.click(screen.getByRole("button", { name: "开始导入" }));
    expect(submit).toHaveBeenCalledWith([files[0]]);
  });

  it("sends the ZIP without unpacking it in the browser", () => {
    const archive = vi.fn();
    const { container } = render(<LibraryImportPicker busy={false} onFiles={vi.fn()} onArchive={archive} />);
    container.querySelector("details")!.open = true;
    const file = new File(["zip"], "papers.zip");
    fireEvent.change(screen.getByLabelText("选择 ZIP"), { target: { files: [file] } });
    fireEvent.click(screen.getByRole("button", { name: "开始导入" }));
    expect(archive).toHaveBeenCalledWith(file);
  });

  it("explains empty folder selection and blocks oversized PDFs", () => {
    const submit = vi.fn();
    const { container } = render(<LibraryImportPicker busy={false} onFiles={submit} onArchive={vi.fn()} />);
    container.querySelector("details")!.open = true;
    fireEvent.change(screen.getByLabelText("选择文件夹"), { target: { files: [new File(["{}"], "metadata.json")] } });
    expect(screen.getByRole("alert")).toHaveTextContent("没有找到 PDF");
    expect(screen.getByRole("button", { name: "开始导入" })).toBeDisabled();
    const file = new File(["x"], "large.pdf");
    Object.defineProperty(file, "size", { value: 81 * 1024 * 1024 });
    fireEvent.change(screen.getByLabelText("选择 PDF"), { target: { files: [file] } });
    expect(screen.getByRole("button", { name: "开始导入" })).toBeDisabled();
    expect(submit).not.toHaveBeenCalled();
  });
});
