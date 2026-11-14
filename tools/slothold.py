"""(14 Nov) Hold one send slot of the kw lanes so at most 2 batches write to the shop at once (the shop gave
HTTP 500s with 3 sends + new categories). Kill this process to give the slot back."""
import fcntl, time
f = open("/data/lanes/kw1/sendslot-2", "w")
fcntl.flock(f, fcntl.LOCK_EX)                 # waits until the batch sending on it now is done
print(time.strftime("%H:%M"), "slot 2 held", flush=True)
while True:
    time.sleep(3600)
