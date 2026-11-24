"""Snapshot-local interval index for unambiguous whole-PGM grouping.

Only containment qualifies. Repeated takes from the same recording must remain
separate, and two possible containing groups are deliberately ambiguous.
"""
from __future__ import annotations


class _Node:
    def __init__(self, group, number):
        self.group, self.key = group, (group['first'], number)
        # Deterministic mixed priorities avoid a degenerate sorted-input tree.
        value = (number + 1) * 0x9E3779B97F4A7C15 & ((1 << 64)-1)
        value = (value ^ (value >> 30)) * 0xBF58476D1CE4E5B9 & ((1 << 64)-1)
        self.priority = value ^ (value >> 27)
        self.left = self.right = None
        self.refresh()

    def refresh(self):
        self.min_first = self.max_first = self.key[0]
        self.min_last = self.max_last = self.group['last']
        self.common_sessions = {row['key'][0] for row in self.group['takes']}
        for child in (self.left,self.right):
            if child:
                self.min_first = min(self.min_first,child.min_first)
                self.max_first = max(self.max_first,child.max_first)
                self.min_last = min(self.min_last,child.min_last)
                self.max_last = max(self.max_last,child.max_last)
                self.common_sessions.intersection_update(child.common_sessions)
        return self


def _split(root, key):
    if root is None: return None,None
    if root.key < key:
        root.right,right = _split(root.right,key)
        return root.refresh(),right
    left,root.left = _split(root.left,key)
    return left,root.refresh()


def _merge(left,right):
    if left is None:return right
    if right is None:return left
    if left.priority > right.priority:
        left.right = _merge(left.right,right)
        return left.refresh()
    right.left = _merge(left,right.left)
    return right.refresh()


def _insert(root,node):
    if root is None:return node
    if node.priority > root.priority:
        node.left,node.right = _split(root,node.key)
        return node.refresh()
    if node.key < root.key:root.left = _insert(root.left,node)
    else:root.right = _insert(root.right,node)
    return root.refresh()


def _remove(root,key):
    if root.key == key:return _merge(root.left,root.right)
    if key < root.key:root.left = _remove(root.left,key)
    else:root.right = _remove(root.right,key)
    return root.refresh()


class CoverageGroupIndex:
    def __init__(self):
        self.roots,self.nodes = {},{}
        self.visited = 0

    def add(self,group):
        if group['first'] is None:return
        family = group['family']
        node = _Node(group,len(self.nodes))
        self.nodes[id(group)] = node
        self.roots[family] = _insert(self.roots.get(family),node)

    def changed(self,group):
        node = self.nodes[id(group)]
        family = group['family']
        self.roots[family] = _remove(self.roots[family],node.key)
        node.key = (group['first'],node.key[1])
        node.left = node.right = None
        self.roots[family] = _insert(self.roots[family],node.refresh())

    def compatible(self,take):
        """Return at most two matches: callers only distinguish zero/one/many."""
        found = []
        first,last,session = take['first'],take['last'],take['key'][0]
        def visit(node):
            if node is None or len(found)>=2:return
            self.visited += 1
            if session in node.common_sessions:return
            # Either a group contains this take, or this take contains a group.
            contains = node.min_first <= first and node.max_last >= last
            contained = node.max_first >= first and node.min_first <= last and node.min_last <= last
            if not (contains or contained):return
            group = node.group
            if (session not in {row['key'][0] for row in group['takes']} and
                    (group['first']<=first<=last<=group['last'] or first<=group['first']<=group['last']<=last)):
                found.append(group)
            visit(node.left);visit(node.right)
        visit(self.roots.get(take['family']))
        return found
