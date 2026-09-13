The `test` workflow builds the images (obuspa from source, ~5–8 min cold),
starts the stack, and runs the whole suite — including the tests that flash
the SD card, since the runner has Docker.
