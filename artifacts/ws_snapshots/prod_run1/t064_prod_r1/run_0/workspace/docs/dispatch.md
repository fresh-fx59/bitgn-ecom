# Dispatch Planning

Use this guide when a request asks you to plan dispatch and points to a dispatch
wave `.md` file.

Read the wave file first. It names the package TSV and lane TSV for that wave.
Return only one JSON object with one assignment per package:

```json
{
  "assignments": [
    {"package_id": "XFER-001", "route": ["lane-a", "lane-b"], "priority": 1}
  ]
}
```

Package rows define the item to move, the source store, the destination store,
the delivery due time, and the margin earned if the package arrives.

Lane rows define directed transport links. Each lane has an origin, destination,
capacity per trip, ETA, trip cost, and `delay_hint`. The delay hint summarizes
past delay observations.

Routes must start at the package `from_store_id` and end at `to_store_id`. A
route may use direct lanes or multiple hub lanes, but every consecutive lane
must connect.

Lower priority numbers load first within each lane queue. Use priorities to
choose which packages should get scarce early capacity when several assignments
share a lane.

Maximize expected net profit, not just the number of delivered packages.

Note that late and missed packages incur penalty: per delay time, and per missed package.

