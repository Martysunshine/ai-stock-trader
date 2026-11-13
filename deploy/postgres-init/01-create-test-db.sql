-- Executed once by the postgres image on first initialisation of an empty volume.
-- Creates the isolated database used by the pytest suite (tests create and drop
-- their own schemas inside it). Owner defaults to POSTGRES_USER.
CREATE DATABASE trader_test;
