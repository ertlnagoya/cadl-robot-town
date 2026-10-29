# Service KPIs

Written by a human (the operator of the town's delivery service) in natural language.
`python -m town.steward --lang en` re-reads this file at the start of every period and uses,
as the KPI of period k, the section whose "## From period k" heading has the largest number
not above k. You may edit it between periods.

## From period 1

Deliver as many orders as possible across the town within the times we promised customers
(120 seconds from order to pickup, 120 seconds from pickup to delivery). No company's customers
should be kept waiting badly.

## From period 5

The quiet season begins and orders fall. On-time delivery still matters, but to keep the
companies cooperating, no single company should gain or lose because of how the road is used.
