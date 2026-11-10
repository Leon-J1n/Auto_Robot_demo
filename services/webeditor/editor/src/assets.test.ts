import { describe, expect, it } from "vitest";
import { GROUP_LABELS, LIBRARY_KINDS, UNGROUPED, categoryLabel, groupAssets } from "./assets";
import type { LibraryItem } from "./library";

const item = (id: number, name: string, category?: string): LibraryItem =>
  ({ id, name, ...(category ? { category } : {}) });

describe("groupAssets", () => {
  it("follows the order /listCategory publishes (§4.6)", () => {
    const groups = groupAssets(
      [item(3, "crate", "模型.容器"), item(0, "apple", "模型.抓取物"), item(2, "table", "模型.家具")],
      ["模型.抓取物", "模型.家具", "模型.容器"],
    );
    expect(groups.map((group) => group.category)).toEqual(["模型.抓取物", "模型.家具", "模型.容器"]);
    expect(groups[1]!.items.map((entry) => entry.name)).toEqual(["table"]);
  });

  it("puts an item without a category in 未分类, last, and never fails (§6.11)", () => {
    const groups = groupAssets([item(9, "loose"), item(0, "apple", "模型.抓取物")], ["模型.抓取物"]);
    expect(groups.map((group) => group.category)).toEqual(["模型.抓取物", UNGROUPED]);
  });

  it("keeps a category the list does not name and drops the ones with no items", () => {
    const groups = groupAssets([item(1, "ur5e", "机械臂.六轴")], ["机械臂.七轴", "机械臂.六轴", "机械臂.SCARA"]);
    expect(groups.map((group) => group.category)).toEqual(["机械臂.六轴"]);
  });

  it("sorts a category missing from the list before 未分类", () => {
    const groups = groupAssets([item(1, "a", "自定义"), item(2, "b"), item(3, "c", "已知")], ["已知"]);
    expect(groups.map((group) => group.category)).toEqual(["已知", "自定义", UNGROUPED]);
  });
});

describe("categoryLabel", () => {
  it("shows the part after the 大类 the first rail already names (§4.6 Q93)", () => {
    expect(categoryLabel("模型.抓取物")).toBe("抓取物");
    expect(categoryLabel("机械臂.六轴")).toBe("六轴");
  });

  it("leaves a dotted-free category and 未分类 alone", () => {
    expect(categoryLabel("自定义")).toBe("自定义");
    expect(categoryLabel(UNGROUPED)).toBe(UNGROUPED);
    expect(categoryLabel("机械臂.")).toBe("机械臂.");
  });

  it("keeps the rest of a deeper path, because the column is scoped to one 大类", () => {
    expect(categoryLabel("机械臂.六轴.UR5")).toBe("六轴.UR5");
  });
});

describe("the panel's groups", () => {
  it("covers the three id spaces in display order", () => {
    expect(LIBRARY_KINDS).toEqual(["scene", "model", "robot"]);
    expect(LIBRARY_KINDS.map((kind) => GROUP_LABELS[kind])).toEqual(["预设场景", "预制模型", "机械臂"]);
  });
});
