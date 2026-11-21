package crvsmoke;

import com.datastax.driver.mapping.annotations.Column;
import com.datastax.driver.mapping.annotations.PartitionKey;
import com.datastax.driver.mapping.annotations.Table;

@Table(keyspace = "crv_smoke_java3", name = "items")
public class Item
{
    @PartitionKey
    private int id;
    @Column(name = "value")
    private String value;

    public Item()
    {
    }

    public Item(int id, String value)
    {
        this.id = id;
        this.value = value;
    }

    public int getId()
    {
        return id;
    }

    public void setId(int id)
    {
        this.id = id;
    }

    public String getValue()
    {
        return value;
    }

    public void setValue(String value)
    {
        this.value = value;
    }
}
