/**
 * The asset panel's view of the object library (spec v2.1 §4.6, §6.11, §12.7).
 * Every list shown here is OLS data: the panel adds objects to the world, it never
 * uploads, deletes or edits what the library publishes.
 */
import type { LibraryItem, LibraryKind } from "./library";

/** The three groups of the panel are the three id spaces OLS publishes (§6.11). */
export const LIBRARY_KINDS: LibraryKind[] = ["scene", "model", "robot"];

/**
 * §4.7 (Q159): the 大类 are named the way OLS names them - the first level of its own
 * `category` strings. These are that word for each id space, used until OLS answers and
 * whenever it cannot be reached; `kindLabel` reads the live one.
 */
export const GROUP_LABELS: Record<LibraryKind, string> = { scene: "场景", model: "模型", robot: "机器人" };

/**
 * §4.7 (Q159): the navigation rail must say what OLS's first level says (`机器人.单臂` -> `机器人`),
 * not a word the frontend invented. The first dotted category decides, in the order `/listCategory`
 * published; a list with no dotted category (or no list yet) falls back to the declared name.
 */
export function kindLabel(kind: LibraryKind, categories: string[]): string {
  for (const category of categories) {
    const dot = category.indexOf(".");
    if (dot > 0) return category.slice(0, dot);
  }
  return GROUP_LABELS[kind];
}

/** §4.6: an item the index gives no category to is 未分类, which is not an error. */
export const UNGROUPED = "未分类";

export interface AssetGroup {
  category: string;
  items: LibraryItem[];
}

/**
 * Group by `category` in the order `/listCategory` publishes (§4.6). Categories with
 * no items are left out, categories the list does not name keep their own group, and
 * the unclassified bucket always sorts last.
 */
export function groupAssets(items: LibraryItem[], categories: string[]): AssetGroup[] {
  const buckets = new Map<string, LibraryItem[]>();
  for (const item of items) {
    const category = item.category ?? UNGROUPED;
    const bucket = buckets.get(category);
    if (bucket) bucket.push(item);
    else buckets.set(category, [item]);
  }
  // `listCategory` may name a category twice; the first mention decides the order.
  const rank = (category: string): number => {
    const index = categories.indexOf(category);
    return index < 0 ? categories.length + (category === UNGROUPED ? 1 : 0) : index;
  };
  return [...buckets].map(([category, grouped]) => ({ category, items: grouped, rank: rank(category) }))
    .sort((left, right) => left.rank - right.rank)
    .map(({ category, items: grouped }) => ({ category, items: grouped }));
}

/**
 * §4.6 (Q93): the second rail sits under the 大类 the first rail already named, so it shows
 * the part after the first dot (`模型.抓取物` -> `抓取物`). Categories without a dot, and
 * 未分类, are their own label.
 */
export function categoryLabel(category: string): string {
  const dot = category.indexOf(".");
  return dot < 0 ? category : category.slice(dot + 1) || category;
}
