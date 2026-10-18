# Legal documents

Drafts for review by a lawyer before the public launch (BETA_10 §14). Each carries "Draft, not legally reviewed"
until reviewed. `consent_records` stores the version a person accepted.

| Document | File | Version | For |
|---|---|---|---|
| Seller Terms, with the data-processing clauses | `seller_terms.md` | `seller-terms-2026-10-18-draft1` | Shop owners, accepted at sign-up |
| Privacy Notice | `privacy_notice.md` | `privacy-notice-2026-10-18-draft1` | Owners, staff, people writing to the product's number |
| Customer notice template | `customer_notice_template.md` | `customer-notice-2026-10-18-draft1` | Each shop's customers, at `/{slug}/privacy` |
| Rights page | `rights.md` | `rights-2026-10-18-draft1` | Everyone |

The documents name the product, the provider, its address, the courts, the liability floor, the grievance officer
and the privacy email through placeholders (`{product_name}`, `{provider_name}`, `{provider_address}`,
`{jurisdiction}`, `{liability_floor}`, `{grievance_officer}`, `{privacy_email}`), filled from the deployment's
settings (`REFLEXO_PRODUCT_NAME`, `REFLEXO_PROVIDER_NAME`, ...) when a page is served. The product can be renamed in
one place, and the provider's address and contact details stay out of this public repository.
