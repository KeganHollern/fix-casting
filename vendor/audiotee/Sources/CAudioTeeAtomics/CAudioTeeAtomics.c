#include "CAudioTeeAtomics.h"

#include <stdatomic.h>
#include <stdlib.h>

struct ATQueueState {
  _Atomic uint64_t write_sequence;
  _Atomic uint64_t read_sequence;
  _Atomic bool overflowed;
  _Atomic bool stopped;
};

ATQueueState *ATQueueStateCreate(void) {
  ATQueueState *state = calloc(1, sizeof(ATQueueState));
  if (state == NULL) {
    return NULL;
  }
  atomic_init(&state->write_sequence, 0);
  atomic_init(&state->read_sequence, 0);
  atomic_init(&state->overflowed, false);
  atomic_init(&state->stopped, false);
  return state;
}

void ATQueueStateDestroy(ATQueueState *state) { free(state); }

uint64_t ATQueueLoadWriteAcquire(const ATQueueState *state) {
  return atomic_load_explicit(&state->write_sequence, memory_order_acquire);
}

uint64_t ATQueueLoadReadAcquire(const ATQueueState *state) {
  return atomic_load_explicit(&state->read_sequence, memory_order_acquire);
}

void ATQueuePublishWrite(ATQueueState *state, uint64_t value) {
  atomic_store_explicit(&state->write_sequence, value, memory_order_release);
}

void ATQueuePublishRead(ATQueueState *state, uint64_t value) {
  atomic_store_explicit(&state->read_sequence, value, memory_order_release);
}

void ATQueueMarkOverflow(ATQueueState *state) {
  atomic_store_explicit(&state->overflowed, true, memory_order_release);
}

bool ATQueueIsOverflowed(const ATQueueState *state) {
  return atomic_load_explicit(&state->overflowed, memory_order_acquire);
}

void ATQueueMarkStopped(ATQueueState *state) {
  atomic_store_explicit(&state->stopped, true, memory_order_release);
}

bool ATQueueIsStopped(const ATQueueState *state) {
  return atomic_load_explicit(&state->stopped, memory_order_acquire);
}
