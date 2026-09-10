# v3 API — endpoints and field names

Read from the live Zephyr Glass tenant on 2026-09-10 with a
read-only snapshot. **Field names and shapes only — no customer data.**

## Endpoints established

| Endpoint | Methods | Notes |
|---|---|---|
| `/api/V1/jobs` | GET, POST | GET lists every job. **POST creates one.** |
| `/api/V1/jobs/{key}` | GET, PUT, DELETE | |
| `/api/V1/jobs/search` | POST | An empty body returns an empty list — do not use it to enumerate. |
| `/api/V1/customers` | GET, POST | |
| `/api/V1/customers/{key}` | GET, PUT, DELETE | Refuses to delete the tenant's default customer. |
| `/api/V1/products`, `/api/V1/locations/names/`, `/api/V1/pricing-profiles`, `/api/V1/customers/paymentterms/`, `/api/V1/identity/users` | GET | Reference data. |

Responses are wrapped: `{data, messages, succeeded}`. **A refusal can arrive as
HTTP 200 with `succeeded: false`** — the status code alone is not the outcome.

## Job

A job carries the customer denormalised (`customerFirstName`, `customerLastName`,
`customerEmail`, `customerPhone`, `customerDisplayName`) *as well as* a
`customerKey`, and has `externalId` / `externalAccountingId` for recording the
originating v2 id.

| Field | Type |
|---|---|
| `billingAddress` | obj: address1, address2, addressType, city, country, id, key, latitude, longitude, postalCode, region |
| `causeOfLoss` | str |
| `claimNumber` | str |
| `createdBy` | str |
| `createdByDisplayName` | null |
| `createdOn` | str |
| `customerDisplayName` | str |
| `customerEmail` | str |
| `customerFirstName` | str |
| `customerInvoiceDate` | null |
| `customerKey` | str |
| `customerLaborTotal` | float |
| `customerLastName` | str |
| `customerPartsTotal` | float |
| `customerPhone` | obj: id, key, phoneNumber, phoneType |
| `customerPoNumber` | null |
| `customerTFEnd` | str |
| `customerTFStart` | str |
| `customerTaxTotal` | float |
| `customerTotalPaid` | float |
| `dateOfLoss` | str |
| `deductiblePrice` | float |
| `ediDate` | str |
| `ediSentStatus` | null |
| `ediStatus` | null |
| `externalAccountingId` | str |
| `externalId` | null |
| `files` | list |
| `installAddress` | obj: address1, address2, addressType, city, country, id, key, latitude, longitude, postalCode, region |
| `installDate` | str |
| `installDuration` | float |
| `installType` | int |
| `installer1Key` | null |
| `installer2Key` | null |
| `installer3Key` | null |
| `insuranceAgentKey` | null |
| `insuranceKey` | null |
| `insuranceLaborTotal` | float |
| `insuranceName` | str |
| `insurancePartsTotal` | float |
| `insuranceTaxTotal` | float |
| `insuranceTotalPaid` | float |
| `isBusinessCustomer` | bool |
| `isComplete` | bool |
| `isSignatureOnFile` | bool |
| `jobLineItems` | list |
| `jobType` | int |
| `jobTypeDisplayName` | str |
| `key` | str |
| `laborTaxRate` | float |
| `lastWorkAssignment` | null |
| `localInstaller1` | obj: priority |
| `localInstaller2` | obj: priority |
| `localInstaller3` | obj: priority |
| `localPricingProfile` | obj: additionalRepairPrice, autoAddCalibration, autoAddLabor, autoAddUrethaneKit, clipsDiscount, clipsMarkUpFlat, fC20, firstRepairPrice, hasUrethaneKit, hmnC10, hmnC15, hmnC20, hmnC25, hmnC30, isDefault, key, laborRate, laborType, lastModifiedOn, moldingDiscount, moldingMarkUpFlat, oemDiscount, oemMarkUpFlat, pricingType, profileName, profileType, rainSensorPadDiscount, rainSensorPadMarkUpFlat, recalDualRate, recalDynamicRate, recalStaticRate, temperedDiscount, temperedMarkUpFlat, udP10, udP15, udP20, udP25, udP30, windshieldDiscount, windshieldMarkUpFlat |
| `locationKey` | str |
| `locationLaborTaxRate` | float |
| `locationLogoUrl` | null |
| `locationTaxRate` | float |
| `logs` | list[obj: act, computedDescription, context, description, jobKey, lastModifiedOn, userDisplayName] |
| `notes` | list[obj: content, createdBy, createdOn, deletedBy, deletedOn, isPrintable, jobKey, key] |
| `overrideTaxRate` | bool |
| `paymentTerms` | int |
| `poDate` | null |
| `poNumber` | str |
| `policyNumber` | str |
| `preferredCommMethod` | int |
| `pricingProfileKey` | str |
| `quoteDate` | null |
| `rebate` | float |
| `referralDate` | null |
| `repairLocation` | str |
| `repairOrderNumber` | null |
| `salesRepKey` | null |
| `shippingAddress` | obj: address1, address2, addressType, city, country, id, key, latitude, longitude, postalCode, region |
| `status` | str |
| `taxExempt` | bool |
| `taxExemptNumber` | str |
| `taxRate` | float |
| `tenantId` | str |
| `tenantSpecificJobNumber` | str |
| `tpaId` | null |
| `vehicle` | obj: bodyStyle, make, mileage, model, nagsVehicleId, plate, plateState, unitNumber, vin, year |
| `warrantyParentJobId` | null |
| `workOrderDate` | null |

## Customer

| Field | Type |
|---|---|
| `addressList` | list[obj: address1, address2, addressType, city, country, id, key, latitude, longitude, postalCode, region] |
| `autoStatementEnabled` | bool |
| `balance` | null |
| `contactList` | list[obj: addressList, contactType, emailAddress, firstName, id, isBusiness, key, lastName, phoneList, preferredComMethod, title] |
| `creditLimit` | null |
| `customerFullName` | str |
| `customerId` | str |
| `customerType` | int |
| `ediInsuranceId` | str |
| `emailAddress` | str |
| `externalAccountingId` | str |
| `hasOwingBalance` | null |
| `insuranceName` | null |
| `isActive` | bool |
| `isDefault` | bool |
| `key` | str |
| `notes` | list |
| `paymentTerms` | int |
| `phoneList` | list[obj: id, key, phoneNumber, phoneType] |
| `preferedComMethod` | int |
| `pricingProfileKey` | str |
| `salesRepKey` | null |
| `statementFrequencyDays` | null |
| `statementNextSendDate` | null |
| `statementSkippedDueToFailures` | bool |
| `taxExemptExpDate` | str |
| `taxExemptNo` | str |
| `tpaId` | str |
| `url` | null |


## What a real POST established

Read-only inspection could not answer these; each came from creating a record
in the live tenant and deleting it again.

| Finding | Detail |
|---|---|
| **A job is accepted with no `customerKey`** | `POST /api/V1/jobs` returns 200 / `succeeded: true`. The denormalised approach works. |
| **…but v3 then invents a customer** | It creates a customer from the job's `customerFirstName`/`LastName`/`Email`/`Phone` and links it, and does **not** deduplicate — two jobs for the same person made two customers. Posting 626 jobs would create up to 626 customers on top of any migrated separately. This — the duplication — is why jobs are linked to already-migrated customers first, not the (mistaken) idea that the auto-created records were malformed. |
| `POST /api/V1/jobs` returns the key alone | `{"data": "01a0…", "succeeded": true}` — a bare string, not the record. The customer endpoint returns the object. |
| `customerType` is **required** | "CustomerType is required." Observed: `9` on the tenant default, `7` on a customer v3 auto-created for an individual. The code for a business is unknown. |
| `contactList` is **required** | "Contact list cannot be null." and "At least one contact is required." |
| `phoneType` is a **string** | A number is refused: "Cannot get the value of a token type 'Number' as a string." Not a strict enum — an invented value was accepted. The tenant's own records use `"Mobile"`. |
| `addressType` is an **integer** | A string is refused: "The JSON value could not be converted to App.Domain.Enums.AddressType". Customers use `3` and `4`; job addresses use `0`. |
| Phone numbers must be **bare digits** | "Phone number must contain only digits." E.164 is refused; v3's own records hold ten digits. |
| A 422 can still leave a record behind | A customer rejected for a bad phone number was found in the tenant afterwards, so validation does not look fully transactional. Check after a failed run rather than assuming nothing was written. |

### `customerId` is null on every created customer — and that is normal

A migrated (or API-created) customer comes back with `customerId: null` where
the tenant default shows `CUST-0001`. This was mistaken, at first, for the
migration producing second-class records. It is not: the shop owner confirmed
that a customer created by hand through the v3 UI **also** has no `customerId`.
So v3 does not assign that human-facing sequence number at creation time, by
any path, and its absence says nothing about whether a record is sound.

The migrator never depended on it regardless: `key` (a UUID, always returned in
`data`) is the identifier throughout — the id map, the job→customer link, and
rollback all use it. `customerId` is display only.
