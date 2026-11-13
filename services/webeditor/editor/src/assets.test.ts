import { describe, expect, it } from "vitest";
import { GROUP_LABELS, LIBRARY_KINDS, UNGROUPED, categoryLabel, groupAssets, kindLabel } from "./assets";
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
    expect(LIBRARY_KINDS.map((kind) => GROUP_LABELS[kind])).toEqual(["场景", "模型", "机器人"]);
  });
});

/**
 * §4.7 (Q159): the rail says OLS's first level, not a word the frontend made up. The strings
 * below are the ones the installed library actually publishes - note `机器人`, which the
 * interface doc still calls `机械臂`: the data is the authority, not the prose.
 */
describe("the rail's word comes from OLS's first level (§4.7, Q159)", () => {
  it("reads the first level of the categories OLS published, per id space", () => {
    expect(kindLabel("scene", ["场景.桌面操作"])).toBe("场景");
    expect(kindLabel("model", ["模型.果蔬", "模型.玩具", "模型.生活用品"])).toBe("模型");
    expect(kindLabel("robot", ["机器人.单臂", "机器人.夹爪", "机器人.摄像机"])).toBe("机器人");
    // The declared fallback happens to be the same word today, so a renamed first level is the
    // only case that proves the label was read from OLS rather than from the constant.
    expect(kindLabel("scene", ["现场.桌面操作"])).toBe("现场");
  });

  it("follows OLS when it renames a 大类, instead of keeping the frontend's word", () => {
    expect(kindLabel("robot", ["机械臂.单臂"])).toBe("机械臂");
    expect(kindLabel("model", ["零件.紧固件"])).toBe("零件");
  });

  it("falls back to the declared name when there is no first level to read", () => {
    // No answer yet, an unreachable library, or categories published without a dot.
    expect(kindLabel("robot", [])).toBe("机器人");
    expect(kindLabel("model", ["自定义"])).toBe("模型");
    expect(kindLabel("scene", [".桌面操作"])).toBe("场景");
  });

  it("takes the first dotted category, so an unclassified entry cannot rename the tab", () => {
    expect(kindLabel("model", ["自定义", "零件.紧固件"])).toBe("零件");
    expect(kindLabel("robot", ["机械臂.单臂", "零件.紧固件"])).toBe("机械臂");
  });
});
