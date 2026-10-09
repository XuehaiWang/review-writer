import { expect, it } from "vitest";
import { tableMarkdown } from "./InlineManuscriptText";

function table(html: string): HTMLElement {
  const host = document.createElement("div");
  host.innerHTML = html;
  return host.querySelector("table")!;
}

it("serializes an edited draft table back to the GFM the manuscript stores", () => {
  const markdown = tableMarkdown(table(`
    <table><thead><tr><th>Catalyst</th><th>Yield</th></tr></thead>
    <tbody><tr><td>CuBr</td><td>88%</td></tr><tr><td>ZnI<sub>2</sub></td><td>71%</td></tr></tbody></table>`));
  // The header row, the GFM separator and every body row must survive, or the
  // saved block would stop being a table.
  expect(markdown.split("\n")).toEqual([
    "| Catalyst | Yield |",
    "| --- | --- |",
    "| CuBr | 88% |",
    "| ZnI2 | 71% |",
  ]);
});

it("keeps row shape when a row has fewer cells than the header", () => {
  expect(tableMarkdown(table(`
    <table><thead><tr><th>A</th><th>B</th><th>C</th></tr></thead>
    <tbody><tr><td>1</td><td>2</td></tr></tbody></table>`)).split("\n")).toEqual([
    "| A | B | C |",
    "| --- | --- | --- |",
    "| 1 | 2 |  |",
  ]);
});

it("escapes a pipe typed inside a cell so the table stays parseable", () => {
  expect(tableMarkdown(table(`
    <table><thead><tr><th>A</th></tr></thead>
    <tbody><tr><td>a|b</td></tr></tbody></table>`)).split("\n")).toEqual([
    "| A |",
    "| --- |",
    "| a\\|b |",
  ]);
});
