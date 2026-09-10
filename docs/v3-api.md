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

