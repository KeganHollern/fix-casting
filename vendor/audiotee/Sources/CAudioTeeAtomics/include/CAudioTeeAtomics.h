#ifndef CAUDIOTEE_ATOMICS_H
#define CAUDIOTEE_ATOMICS_H

#include <stdbool.h>
#include <stdint.h>

typedef struct ATQueueState ATQueueState;

ATQueueState *ATQueueStateCreate(void);
void ATQueueStateDestroy(ATQueueState *state);

uint64_t ATQueueLoadWriteAcquire(const ATQueueState *state);
uint64_t ATQueueLoadReadAcquire(const ATQueueState *state);
void ATQueuePublishWrite(ATQueueState *state, uint64_t value);
void ATQueuePublishRead(ATQueueState *state, uint64_t value);

void ATQueueMarkOverflow(ATQueueState *state);
bool ATQueueIsOverflowed(const ATQueueState *state);
void ATQueueMarkStopped(ATQueueState *state);
bool ATQueueIsStopped(const ATQueueState *state);

#endif
