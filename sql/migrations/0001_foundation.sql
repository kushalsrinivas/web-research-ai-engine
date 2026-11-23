-- rollback: forward-fix only. Removing these schemas or extensions would destroy
-- shared state; create a new migration that stops using them instead.

create schema if not exists core;
create schema if not exists ingest;
create schema if not exists fetcher;
create schema if not exists parse;
create schema if not exists search;
create schema if not exists graph;
create schema if not exists research;
create schema if not exists llm;
create schema if not exists ops;
create schema if not exists api;

create extension if not exists vector;
create extension if not exists pg_trgm;
