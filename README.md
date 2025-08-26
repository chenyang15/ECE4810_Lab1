# IoT Theme Park Monitoring & Ride Management System
This repository contains source code for two case studies that demonstrate the use of Raspberry Pi, ultrasonic sensors, MQTT, ThingSpeak, and Telegram integration for real-time IoT monitoring in a theme park environment.
Repository Structure

*Case A — Theme Park Path Routing Detection*
-caseA_master.py: Aggregator node that collects data from all gates (A_in, B_in, A2B, B2A) and its own Exit gate. Publishes crowd counts to ThingSpeak and responds to Telegram /status queries.
-caseA_slave.py: Slave node code for gate Raspberry Pis. Each slave detects visitors using an ultrasonic sensor and publishes counts every interval via MQTT.

*Case B — Ride Queue & Seat Management*
-caseB_height_detection.py: Height sensor logic ensuring riders meet minimum height requirements. Publishes status via MQTT, pushes data to ThingSpeak, and responds to Telegram bot commands.
-caseB_queue_wait_time.py: Queue wait-time estimation using distance bands. Publishes estimated wait time values (5/10/15 minutes) per second to MQTT.
-caseB_ride_seat_detection.py: Ride controller handling boarding, ride operation, and unloading. Tracks seat occupancy using entrance/exit ultrasonic sensors and publishes ride state (green/yellow/red lights + seat count) to MQTT.
