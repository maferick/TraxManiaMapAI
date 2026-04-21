-- Free-block support. GBX.NET distinguishes two placement modes:
--   grid blocks: integer cell coords (Int3 Coord)
--   free blocks: world-space abs position (Vec3 AbsolutePositionInMap)
--                + yaw/pitch/roll (Vec3 YawPitchRoll)
-- A single map freely mixes both. Prior to this migration the table
-- assumed grid only; x/y/z were NOT NULL. Making them nullable with an
-- is_free discriminator keeps both kinds under one row shape.
--
-- Grid rows:  is_free = 0, x/y/z NOT NULL,  abs_*/yaw/pitch/roll NULL
-- Free rows:  is_free = 1, x/y/z NULL,      abs_*/yaw/pitch/roll set

ALTER TABLE block_placements
    MODIFY COLUMN x INT NULL,
    MODIFY COLUMN y INT NULL,
    MODIFY COLUMN z INT NULL,
    ADD COLUMN is_free  TINYINT(1)    NOT NULL DEFAULT 0 AFTER rotation,
    ADD COLUMN abs_x    DECIMAL(12,3) NULL     AFTER is_free,
    ADD COLUMN abs_y    DECIMAL(12,3) NULL     AFTER abs_x,
    ADD COLUMN abs_z    DECIMAL(12,3) NULL     AFTER abs_y,
    ADD COLUMN yaw      FLOAT         NULL     AFTER abs_z,
    ADD COLUMN pitch    FLOAT         NULL     AFTER yaw,
    ADD COLUMN roll     FLOAT         NULL     AFTER pitch;
