# Design QA

- Source visual truth: `C:\Users\Admin\AppData\Local\Temp\codex-clipboard-bdd19d08-2e87-4888-8e67-75da1fe26004.png`
- Implementation screenshot: unavailable
- Viewport: desktop, approximately 1499 x 982
- State: monitoring list with an open row action menu

## Full-view comparison evidence

The source screenshot shows a broken action column: row boundaries step around the
action cells and the open menu participates in the table layout. The implementation
was changed so the `td` remains a table cell and the menu renders as a fixed overlay.

## Focused region comparison evidence

The relevant region is the right edge of the monitoring table. Code inspection
confirmed that `.mrm-row-actions` applied `display: flex` directly to the `td`, which
removed it from normal table-cell layout. The class was removed from the cell.

## Findings

- Visual confirmation is blocked because the authenticated Nextcloud page was not
  available in the in-app browser session.
- No remaining code-level P0/P1/P2 issue was found in the changed table and menu
  styles.

## Patches made

- Restored the action `td` to normal table-cell layout.
- Replaced the sticky action column with a fixed-width standard table column.
- Rendered the action dropdown in a fixed overlay outside the table.
- Closed the overlay on scroll and viewport resize.
- Removed the last-row bottom border to keep the panel edge clean.

final result: blocked
