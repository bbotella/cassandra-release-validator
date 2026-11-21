/*
 * Uses cassandra-all as a library, the way bulk loaders do: writes SSTables with CQLSSTableWriter.
 * Arguments: <directory> <rows>. Prints the smoke protocol (CRV-STEP ... / CRV-DONE).
 */

import java.io.File;

import org.apache.cassandra.io.sstable.CQLSSTableWriter;

public class CassandraAllSmoke
{
    public static void main(String[] args)
    {
        String dir = args[0];
        int rows = Integer.parseInt(args[1]);
        try
        {
            new File(dir).mkdirs();
            CQLSSTableWriter writer = CQLSSTableWriter.builder()
                                                      .inDirectory(dir)
                                                      .forTable("CREATE TABLE crv_smoke.imported (id int PRIMARY KEY, value text)")
                                                      .using("INSERT INTO crv_smoke.imported (id, value) VALUES (?, ?)")
                                                      .build();
            for (int i = 0; i < rows; i++)
                writer.addRow(i, "value-" + i);
            writer.close();
            String[] data = new File(dir).list((d, name) -> name.endsWith("-Data.db"));
            if (data != null && data.length > 0)
                System.out.println("CRV-STEP write-sstables OK " + rows + " rows in " + data.length + " SSTable(s)");
            else
                System.out.println("CRV-STEP write-sstables FAIL no -Data.db file in " + dir);
        }
        catch (Throwable t)
        {
            t.printStackTrace(System.out);
            System.out.println("CRV-STEP write-sstables FAIL " + t.toString().replace('\n', ' '));
        }
        System.out.println("CRV-DONE");
        System.exit(0);  // cassandra-all can leave non-daemon threads behind
    }
}
