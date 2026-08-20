/**
 * rehype plugin: wrap inline citation markers like [1] or [2] in a
 * <sup class="citation-ref"> so they render as subtle superscript references.
 *
 * The References list at the bottom of an answer is an ordered Markdown list
 * (1. 2. 3.), so its numbers are list markers — not bracketed text — and are
 * left untouched. Link text (inside <a>) is skipped so URLs aren't mangled.
 *
 * Dependency-free: walks the hast tree directly instead of pulling in
 * unist-util-visit.
 */
const CITATION = /\[(\d+)\]/g

function transformChildren(node) {
  if (!node.children || node.children.length === 0) return

  const next = []
  for (const child of node.children) {
    if (child.type === 'text' && CITATION.test(child.value)) {
      CITATION.lastIndex = 0
      let last = 0
      let match
      while ((match = CITATION.exec(child.value)) !== null) {
        if (match.index > last) {
          next.push({ type: 'text', value: child.value.slice(last, match.index) })
        }
        next.push({
          type: 'element',
          tagName: 'sup',
          properties: { className: ['citation-ref'] },
          children: [{ type: 'text', value: match[0] }],
        })
        last = match.index + match[0].length
      }
      if (last < child.value.length) {
        next.push({ type: 'text', value: child.value.slice(last) })
      }
    } else {
      // Don't recurse into links; recurse everywhere else.
      if (child.tagName !== 'a') transformChildren(child)
      next.push(child)
    }
  }
  node.children = next
}

export default function rehypeCitations() {
  return (tree) => transformChildren(tree)
}
