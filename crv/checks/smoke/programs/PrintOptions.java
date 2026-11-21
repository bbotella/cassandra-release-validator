/*
 * Prints the JVM options a Spark driver needs to run Cassandra Analytics on this JDK, from the release itself:
 * BulkSparkConf.JDK11_OPTIONS (and JDK17_OPTIONS where the release has it), plus Spark's own module options
 * (what spark-submit adds on JDK 17+). Argument: <JDK major version>.
 */

import java.lang.reflect.Field;

import org.apache.cassandra.spark.bulkwriter.BulkSparkConf;

public class PrintOptions
{
    public static void main(String[] args) throws Exception
    {
        int jdk = Integer.parseInt(args[0]);
        StringBuilder options = new StringBuilder(BulkSparkConf.JDK11_OPTIONS);
        if (jdk >= 17)
        {
            try
            {
                Field field = BulkSparkConf.class.getField("JDK17_OPTIONS");
                options.append(' ').append(field.get(null));
            }
            catch (NoSuchFieldException ignored)
            {
                // releases built for JDK 11 only
            }
            try
            {
                Class<?> spark = Class.forName("org.apache.spark.launcher.JavaModuleOptions");
                options.append(' ').append(spark.getMethod("defaultModuleOptions").invoke(null));
            }
            catch (ClassNotFoundException ignored)
            {
                // Spark before 3.3 has no module options
            }
        }
        System.out.println(options.toString().trim());
    }
}
