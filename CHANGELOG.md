# Changelog

All notable changes to Engram will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [1.0.0] - 2026-04-19

### Added
- SQLite database with FTS5 full-text search
- CLI tool (`engram`) with commands: search, recent, add, list, link-pattern, stats, init, seed
- MCP server for Cursor IDE and Claude Desktop integration
- 8 MCP tools: memory_search, memory_recent, memory_add_mistake, memory_add_pattern, memory_add_skill, memory_add_conversation, memory_list, memory_stats
- Cursor rule file for automatic agent integration
- Docker support (Dockerfile + docker-compose.yml)
- Install script with shell function setup
- 4 memory types: mistakes, patterns, skills, conversations
- Tag system with cross-cutting labels
- WAL mode for concurrent reads
- Seed module with sample data from real development sessions
