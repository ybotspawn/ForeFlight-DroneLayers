# ForeFlight-DroneLayers
Drone Airspace → ForeFlight Automated Starting with AIRAC-cycle KMZ layers built from official NATS ENR 5.1 data, ready to import into ForeFlight as toggleable drone restriction overlays; but eventually expanded to all of Europe

## Operation
This repository contains both the code to generate new layers as well as regularly updated layers (under the layers folder).  You can download and run this code manually, or you can download the kmz files directly from the layers folder for the region you need and import them directly to foreflight

## Sources
UK - Generate script utilizes NATS as a direct source for drone flight restrictions
US - Generate script utilizes FAA as a direct source for drone flight restrictions
EU/CA - Generate script utilizes data curated and obtained free from OpenAIP. (https://www.openaip.net/)

## Not Covered
The layers in this repository do not cover the following (there could be more depending on your usage case, RP is always required to verify before flight)
Non-airspace ground hazards not in ENR 5.1: SSSIs, National Trust land, land access/permission, local byelaws, schools/sports venues as ground-risk features. These aren't airspace — they're property/privacy/ground-risk considerations the CAA expects you to assess in your site survey narrative.
DJI GEO zones if you fly DJI hardware. These are vendor geofences independent of legal airspace and can stop you taking off even where it's legal.
FRZ permission submission — ForeFlight won't route the request. You still need the aerodrome's published contact or, where available, Altitude Angel GuardianUTM.
Atypical Air Environments / swarm zones and temporary restricted airspace (RATs, TDAs) — these do end up in NOTAMs so ForeFlight catches them, but worth knowing the terms exist.
