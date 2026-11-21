/*
 * Uses the Sidecar Java client the way a client application does, against a running Sidecar on port 9043.
 * Arguments: <node host name> <expected Sidecar version>. Prints the smoke protocol (CRV-STEP ... / CRV-DONE).
 */

import java.util.Collections;

import io.vertx.core.Vertx;
import org.apache.cassandra.sidecar.client.HttpClientConfig;
import org.apache.cassandra.sidecar.client.SidecarClient;
import org.apache.cassandra.sidecar.client.SidecarClientConfig;
import org.apache.cassandra.sidecar.client.SidecarClientConfigImpl;
import org.apache.cassandra.sidecar.client.SidecarInstanceImpl;
import org.apache.cassandra.sidecar.client.SimpleSidecarInstancesProvider;
import org.apache.cassandra.sidecar.client.VertxHttpClient;
import org.apache.cassandra.sidecar.client.VertxRequestExecutor;
import org.apache.cassandra.sidecar.client.retry.ExponentialBackoffRetryPolicy;
import org.apache.cassandra.sidecar.common.response.HealthResponse;
import org.apache.cassandra.sidecar.common.response.NodeSettings;
import org.apache.cassandra.sidecar.common.response.SchemaResponse;

public class SidecarClientSmoke
{
    static void step(String name, boolean ok, String detail)
    {
        System.out.println("CRV-STEP " + name + " " + (ok ? "OK" : "FAIL") + " " + detail.replace('\n', ' '));
    }

    public static void main(String[] args)
    {
        String host = args[0];
        String expected = args[1];
        Vertx vertx = Vertx.vertx();
        try
        {
            HttpClientConfig http = new HttpClientConfig.Builder<>().ssl(false).timeoutMillis(10_000).build();
            SidecarClientConfig config = SidecarClientConfigImpl.builder().maxRetries(3).build();
            SidecarInstanceImpl instance = new SidecarInstanceImpl(host, 9043);
            try (SidecarClient client = new SidecarClient(new SimpleSidecarInstancesProvider(Collections.singletonList(instance)),
                                                          new VertxRequestExecutor(new VertxHttpClient(vertx, http)),
                                                          config,
                                                          new ExponentialBackoffRetryPolicy(3, 500, 5_000)))
            {
                HealthResponse health = client.sidecarHealth().get();
                step("health", health.isOk(), "sidecarHealth(): " + health.status());
                NodeSettings settings = client.nodeSettings().get();
                step("node-settings", expected.equals(settings.sidecarVersion()),
                     "nodeSettings(): Sidecar " + settings.sidecarVersion() + ", Cassandra " + settings.releaseVersion());
                SchemaResponse schema = client.fullSchema().get();
                step("schema", schema.schema() != null && schema.schema().contains("system"),
                     "fullSchema(): " + (schema.schema() == null ? 0 : schema.schema().length()) + " characters");
            }
        }
        catch (Throwable t)
        {
            t.printStackTrace(System.out);
            step("client", false, t.toString());
        }
        vertx.close();
        System.out.println("CRV-DONE");
        System.exit(0);
    }
}
