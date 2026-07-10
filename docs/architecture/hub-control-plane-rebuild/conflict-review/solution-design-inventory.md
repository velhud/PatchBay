# Solution Design Inventory

| Design area | Source | Decision |
| --- | --- | --- |
| Exact public manager surface | `../hub-manager-tool-contract.md` | Keep 31 tools; correct schemas and semantics through addendum |
| Transparent Hub adapter | `../selected-solution-design.md` | Keep |
| Transactional Hub state | selected design plus addendum section 15 | Sequence after identity/contracts |
| Operation broker | selected design plus addendum sections 2-5 | Merge with Edge receipt protocol |
| Worker projections | selected design plus addendum sections 6 and 9 | Required before group close/reassign |
| Group lifecycle | selected design plus addendum section 13 | Sequence after projection/integration |
| Verification | `../implementation-verification-plan.md` | Expand using conflict findings |

All areas are one integrated rebuild. None is safe as an isolated label-only
patch.
