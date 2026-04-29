"""
KV Cache Eviction Offline Simulation Platform
==============================================

Evaluates LRU, TTL-LRU, Two-Queue TTL and other eviction policies
against real request traces for prefix cache hit-rate analysis.

Package layout
--------------
sim.core        — stable data models and abstract interfaces (extend by subclassing)
sim.policies    — eviction policy implementations (extend by adding a file + registry entry)
sim.cache       — prefix cache simulation engine
sim.io          — trace loading and offline registry
sim.metrics     — metric collection and reporting
sim.runner      — orchestration (SimulationRunner)
sim.config      — configuration dataclasses
"""
