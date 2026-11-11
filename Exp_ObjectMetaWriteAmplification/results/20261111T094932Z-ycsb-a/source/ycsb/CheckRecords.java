package lab;

import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Paths;
import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.Future;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.concurrent.atomic.LongAdder;
import site.ycsb.Utils;

/** Read every loaded record directly from the backend, after dirty-cache drain. */
public final class CheckRecords {
  public static void main(String[] args) throws Exception {
    String pool = args[0];
    int count = Integer.parseInt(args[1]);
    int size = Integer.parseInt(args[2]);
    int threads = Integer.parseInt(args[3]);
    long cluster = RadosBlockDB.openCluster("/etc/ceph/ceph.conf", "admin");
    AtomicInteger next = new AtomicInteger();
    LongAdder checked = new LongAdder();
    ExecutorService executor = Executors.newFixedThreadPool(threads);
    List<Future<?>> futures = new ArrayList<>();
    for (int t = 0; t < threads; t++) {
      futures.add(executor.submit(() -> {
        long io = RadosBlockDB.openIoctx(cluster, pool);
        try {
          int index;
          while ((index = next.getAndIncrement()) < count) {
            String key = "usertable/user" + Utils.hash(index);
            byte[] bytes = RadosBlockDB.readFull(io, key, size, true);
            if (bytes.length != size || !RadosBlockDB.valid(bytes)) {
              throw new IllegalStateException("invalid record " + key);
            }
            checked.increment();
          }
        } finally {
          RadosBlockDB.closeIoctx(io);
        }
      }));
    }
    try {
      for (Future<?> future : futures) { future.get(); }
      String result = "{\"records_checked\":" + checked.sum()
          + ",\"bytes_checked\":" + checked.sum() * size
          + ",\"crc_valid\":true,\"bypassed_overlay\":true}\n";
      Files.write(Paths.get(args[4]), result.getBytes(StandardCharsets.UTF_8));
      System.out.print(result);
    } finally {
      executor.shutdownNow();
      RadosBlockDB.closeCluster(cluster);
    }
  }
}
