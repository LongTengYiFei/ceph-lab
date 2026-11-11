package lab;

import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Paths;
import java.util.Map;
import java.util.HashMap;
import java.util.Set;
import java.util.Vector;
import java.util.concurrent.atomic.LongAdder;
import java.util.zip.CRC32;
import site.ycsb.ByteArrayByteIterator;
import site.ycsb.ByteIterator;
import site.ycsb.DB;
import site.ycsb.DBException;
import site.ycsb.Status;

/** Fixed-size, one-field RADOS adapter for official YCSB CoreWorkload.
 * Updates issue rados_write_full directly: no delete/reinsert or JSON encoding.
 * Eight bytes within the 4096-byte record carry a marker and CRC32, verified
 * on every read. These bytes remain part of the counted RADOS payload.
 */
public final class RadosBlockDB extends DB {
  private static long cluster;
  private static int users;
  private static final LongAdder READS = new LongAdder();
  private static final LongAdder UPDATES = new LongAdder();
  private static final LongAdder INSERTS = new LongAdder();
  private static final LongAdder WRITTEN = new LongAdder();
  private static final LongAdder FAILURES = new LongAdder();
  private static final LongAdder BAD_CRC = new LongAdder();
  private static String statsFile;
  private static String phase;
  private long ioctx;
  private int recordBytes;
  private boolean bypass;

  static {
    System.loadLibrary("objmeta_rados");
    Runtime.getRuntime().addShutdownHook(new Thread(() -> {
      if (statsFile != null) {
        String json = String.format(java.util.Locale.ROOT,
            "{\"phase\":\"%s\",\"read_success\":%d,\"update_success\":%d,"
            + "\"insert_success\":%d,\"successful_write_bytes\":%d,"
            + "\"failures\":%d,\"crc_failures\":%d}\n",
            phase, READS.sum(), UPDATES.sum(), INSERTS.sum(), WRITTEN.sum(),
            FAILURES.sum(), BAD_CRC.sum());
        try {
          Files.write(Paths.get(statsFile), json.getBytes(StandardCharsets.UTF_8));
        } catch (IOException error) {
          error.printStackTrace();
        }
      }
    }));
  }

  public static native long openCluster(String config, String id);
  public static native void closeCluster(long handle);
  public static native long openIoctx(long handle, String pool);
  public static native void closeIoctx(long handle);
  public static native int writeFull(long handle, String key, byte[] data, boolean bypass);
  public static native byte[] readFull(long handle, String key, int size, boolean bypass);
  public static native int remove(long handle, String key);

  @Override
  public void init() throws DBException {
    recordBytes = Integer.parseInt(getProperties().getProperty("rados.record_bytes", "4096"));
    bypass = Boolean.parseBoolean(getProperties().getProperty("rados.bypass", "false"));
    if (recordBytes < 16) {
      throw new DBException("record too small");
    }
    synchronized (RadosBlockDB.class) {
      statsFile = getProperties().getProperty("rados.stats");
      phase = getProperties().getProperty("rados.phase", "unknown");
      if (cluster == 0) {
        cluster = openCluster(getProperties().getProperty("rados.config", "/etc/ceph/ceph.conf"),
            getProperties().getProperty("rados.id", "admin"));
      }
      ioctx = openIoctx(cluster, getProperties().getProperty("rados.pool"));
      users++;
    }
  }

  @Override
  public void cleanup() {
    synchronized (RadosBlockDB.class) {
      if (ioctx != 0) {
        closeIoctx(ioctx);
        ioctx = 0;
        users--;
        if (users == 0) {
          closeCluster(cluster);
          cluster = 0;
        }
      }
    }
  }

  public static boolean valid(byte[] bytes) {
    if (bytes.length < 8 || bytes[0] != 'O' || bytes[1] != 'M'
        || bytes[2] != 'W' || bytes[3] != 'A') {
      return false;
    }
    CRC32 crc = new CRC32();
    crc.update(bytes, 8, bytes.length - 8);
    long expected = 0;
    for (int i = 4; i < 8; i++) {
      expected = (expected << 8) | (bytes[i] & 255L);
    }
    return expected == crc.getValue();
  }

  private Status write(String table, String key, Map<String, ByteIterator> values, boolean insert) {
    if (values.size() != 1) {
      throw new IllegalArgumentException("this adapter requires fieldcount=1");
    }
    byte[] bytes = values.values().iterator().next().toArray();
    if (bytes.length != recordBytes) {
      throw new IllegalArgumentException("unexpected payload length");
    }
    bytes[0] = 'O'; bytes[1] = 'M'; bytes[2] = 'W'; bytes[3] = 'A';
    CRC32 crc = new CRC32();
    crc.update(bytes, 8, bytes.length - 8);
    long checksum = crc.getValue();
    for (int i = 7; i >= 4; i--) {
      bytes[i] = (byte) checksum;
      checksum >>>= 8;
    }
    int status = writeFull(ioctx, table + "/" + key, bytes, bypass);
    if (status != 0) {
      if (FAILURES.sum() < 10) {
        System.err.println("RADOS write failed: " + status + " " + key);
      }
      FAILURES.increment();
      return Status.ERROR;
    }
    WRITTEN.add(bytes.length);
    if (insert) { INSERTS.increment(); } else { UPDATES.increment(); }
    return Status.OK;
  }

  @Override
  public Status insert(String table, String key, Map<String, ByteIterator> values) {
    return write(table, key, values, true);
  }

  @Override
  public Status update(String table, String key, Map<String, ByteIterator> values) {
    return write(table, key, values, false);
  }

  @Override
  public Status read(String table, String key, Set<String> fields, Map<String, ByteIterator> result) {
    try {
      byte[] bytes = readFull(ioctx, table + "/" + key, recordBytes, bypass);
      if (bytes.length != recordBytes || !valid(bytes)) {
        BAD_CRC.increment();
        FAILURES.increment();
        return Status.ERROR;
      }
      result.put("field0", new ByteArrayByteIterator(bytes));
      READS.increment();
      return Status.OK;
    } catch (RuntimeException error) {
      if (FAILURES.sum() < 10) { error.printStackTrace(); }
      FAILURES.increment();
      return Status.ERROR;
    }
  }

  @Override
  public Status delete(String table, String key) {
    return remove(ioctx, table + "/" + key) == 0 ? Status.OK : Status.ERROR;
  }

  @Override
  public Status scan(String table, String start, int count, Set<String> fields,
      Vector<HashMap<String, ByteIterator>> ignored) {
    return Status.NOT_IMPLEMENTED;
  }
}
