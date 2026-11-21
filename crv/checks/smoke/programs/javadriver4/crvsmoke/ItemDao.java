package crvsmoke;

import com.datastax.oss.driver.api.mapper.annotations.Dao;
import com.datastax.oss.driver.api.mapper.annotations.Insert;
import com.datastax.oss.driver.api.mapper.annotations.Select;

@Dao
public interface ItemDao
{
    @Insert
    void save(Item item);

    @Select
    Item findById(int id);
}
