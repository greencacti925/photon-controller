/*
 * Copyright 2015 VMware, Inc. All Rights Reserved.
 *
 * Licensed under the Apache License, Version 2.0 (the "License"); you may not
 * use this file except in compliance with the License.  You may obtain a copy of
 * the License at http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software distributed
 * under the License is distributed on an "AS IS" BASIS, without warranties or
 * conditions of any kind, EITHER EXPRESS OR IMPLIED.  See the License for the
 * specific language governing permissions and limitations under the License.
 */

package com.vmware.photon.controller.api;

import com.vmware.photon.controller.api.builders.AttachedDiskCreateSpecBuilder;
import com.vmware.photon.controller.api.helpers.Validator;
import static com.vmware.photon.controller.api.helpers.JsonHelpers.asJson;
import static com.vmware.photon.controller.api.helpers.JsonHelpers.fromJson;
import static com.vmware.photon.controller.api.helpers.JsonHelpers.jsonFixture;

import com.google.common.collect.ImmutableList;
import org.testng.annotations.DataProvider;
import org.testng.annotations.Test;
import static org.hamcrest.MatcherAssert.assertThat;
import static org.hamcrest.Matchers.is;
import static org.hamcrest.core.StringStartsWith.startsWith;
import static uk.co.datumedge.hamcrest.json.SameJSONAs.sameJSONAs;

/**
 * Tests {@link AttachedDiskCreateSpec}.
 */
public class AttachedDiskCreateSpecTest {

  /**
   * Dummy test case to make Intellij recognize this as a test class.
   */
  @Test
  private void dummy() {
  }

  /**
   * Tests for validations.
   */
  public class ValidationTest {

    private Validator validator = new Validator();

    @Test(dataProvider = "validAttachedDiskCreateSpecs")
    public void testValidAttachedDiskCreateSpecs(AttachedDiskCreateSpec spec) {
      ImmutableList<String> violations = validator.validate(spec);
      assertThat(violations.isEmpty(), is(true));
    }

    @DataProvider(name = "validAttachedDiskCreateSpecs")
    private Object[][] getValidAttachedDiskCreateSpecs() {
      return new Object[][]{
          {new AttachedDiskCreateSpecBuilder().name("d").flavor("f").bootDisk(true).build()},
          {new AttachedDiskCreateSpecBuilder().name("d").flavor("f").capacityGb(2).build()},
      };
    }

    @Test(dataProvider = "invalidAttachedDiskCreateSpecs")
    public void testInvalidAttachedDiskCreateSpecs(AttachedDiskCreateSpec spec, String errorMsg) {
      ImmutableList<String> violations = validator.validate(spec);

      assertThat(violations.size(), is(1));
      assertThat(violations.get(0), startsWith(errorMsg));
    }

    @DataProvider(name = "invalidAttachedDiskCreateSpecs")
    private Object[][] getInvalidAttachedDiskCreateSpecs() {
      return new Object[][]{
          {new AttachedDiskCreateSpecBuilder().name("d").flavor("f").capacityGb(-2).build(),
              "capacityGb must be greater than or equal to 1 (was -2)"},
      };
    }
  }

  /**
   * Tests JSON serialization.
   */
  public class SerializationTest {

    @Test
    public void testBootDisk() throws Exception {
      AttachedDiskCreateSpec attachedDiskCreateSpec =
          new AttachedDiskCreateSpecBuilder().name("mydisk").flavor("good-flavor").bootDisk(true).build();

      String json = jsonFixture("fixtures/attached-disk-create-spec-boot-disk.json");

      assertThat(fromJson(json, AttachedDiskCreateSpec.class), is(attachedDiskCreateSpec));
      assertThat(asJson(attachedDiskCreateSpec), is(sameJSONAs(json)));
    }

    @Test
    public void testNonBootDisk() throws Exception {
      AttachedDiskCreateSpec attachedDiskCreateSpec =
          new AttachedDiskCreateSpecBuilder().name("mydisk").flavor("good-flavor").capacityGb(2).build();

      String json = jsonFixture("fixtures/attached-disk-create-spec-non-boot-disk.json");

      assertThat(fromJson(json, AttachedDiskCreateSpec.class), is(attachedDiskCreateSpec));
      assertThat(asJson(attachedDiskCreateSpec), is(sameJSONAs(json)));
    }
  }

}
