import unittest
import torch
from torch import nn

try:
    from pytorch_quantization import nn as quant_nn
    from super_gradients.training.utils.quantization.fine_grain_quantization_utils import QuantizationUtility, \
        RegisterQuantizedModule
    from super_gradients.training.utils.quantization.core import SkipQuantization, SGQuantMixin, QuantizedMapping

    _imported_pytorch_quantization_failure = None

except (ImportError, NameError, ModuleNotFoundError) as import_err:
    _imported_pytorch_quantization_failure = import_err
    print(import_err)


@unittest.skipIf(_imported_pytorch_quantization_failure is not None, "Failed to import `pytorch_quantization`")
class QuantizationUtilityTest(unittest.TestCase):
    def test_vanilla_replacement(self):
        # ARRANGE
        class MyModel(nn.Module):
            def __init__(self) -> None:
                super().__init__()
                self.conv1 = nn.Conv2d(3, 8, kernel_size=3, padding=1)

            def forward(self, x):
                return self.conv1(x)

        module = MyModel()

        # TEST
        q_util = QuantizationUtility()
        q_util.quantize_module(module)

        x = torch.rand(1, 3, 32, 32)

        # ASSERT
        with torch.no_grad():
            y = module(x)
            torch.testing.assert_close(y.size(), (1, 8, 32, 32))

        self.assertTrue(isinstance(module.conv1, QuantizationUtility.mapping_instructions[nn.Conv2d].quantized_type))

    def test_module_list_replacement(self):
        # ARRANGE
        class MyModel(nn.Module):
            def __init__(self) -> None:
                super().__init__()
                self.convs = nn.ModuleList([nn.Conv2d(3, 8, kernel_size=3, padding=1) for _ in range(3)])

            def forward(self, x):
                return torch.cat([conv(x) for conv in self.convs], dim=1)

        module = MyModel()

        # TEST
        q_util = QuantizationUtility()
        q_util.quantize_module(module)

        x = torch.rand(1, 3, 32, 32)

        # ASSERT
        with torch.no_grad():
            y = module(x)
            torch.testing.assert_close(y.size(), (1, 3 * 8, 32, 32))

        for conv in module.convs:
            self.assertTrue(isinstance(conv, QuantizationUtility.mapping_instructions[nn.Conv2d].quantized_type))

    def test_sequential_list_replacement(self):
        # ARRANGE
        class MyModel(nn.Module):
            def __init__(self) -> None:
                super().__init__()
                self.convs = nn.Sequential(
                    nn.Conv2d(3, 8, kernel_size=3, padding=1),
                    nn.Conv2d(8, 16, kernel_size=3, padding=1),
                )

            def forward(self, x):
                return self.convs(x)

        module = MyModel()

        # TEST
        q_util = QuantizationUtility()
        q_util.quantize_module(module)

        x = torch.rand(1, 3, 32, 32)

        # ASSERT
        with torch.no_grad():
            y = module(x)
            torch.testing.assert_close(y.size(), (1, 16, 32, 32))

        for conv in module.convs:
            self.assertTrue(isinstance(conv, QuantizationUtility.mapping_instructions[nn.Conv2d].quantized_type))

    def test_nested_module_replacement(self):
        # ARRANGE
        class MyBlock(nn.Module):
            def __init__(self, in_feats, out_feats) -> None:
                super().__init__()
                self.flatten = nn.Flatten()
                self.linear = nn.Linear(in_feats, out_feats)

            def forward(self, x):
                return self.linear(self.flatten(x))

        class MyModel(nn.Module):
            def __init__(self, res, n_classes) -> None:
                super().__init__()
                self.conv = nn.Conv2d(3, 4, kernel_size=3, padding=1)
                self.my_block = MyBlock(4 * (res ** 2), n_classes)

            def forward(self, x):
                y = self.conv(x)
                return self.my_block(y)

        res = 32
        n_clss = 10
        module = MyModel(res, n_clss)

        # TEST
        q_util = QuantizationUtility()
        q_util.quantize_module(module)

        x = torch.rand(1, 3, res, res)

        # ASSERT
        with torch.no_grad():
            y = module(x)
            torch.testing.assert_close(y.size(), (1, n_clss))

        self.assertTrue(isinstance(module.conv,
                                   QuantizationUtility.mapping_instructions[nn.Conv2d].quantized_type))
        self.assertTrue(isinstance(module.my_block.linear,
                                   QuantizationUtility.mapping_instructions[nn.Linear].quantized_type))

    def test_static_selective_skip_quantization(self):
        # ARRANGE
        class MyModel(nn.Module):
            def __init__(self) -> None:
                super().__init__()
                self.conv1 = nn.Conv2d(3, 8, kernel_size=3, padding=1)
                self.conv2 = SkipQuantization(nn.Conv2d(8, 16, kernel_size=3, padding=1))

            def forward(self, x):
                return self.conv2(self.conv1(x))

        module = MyModel()

        # TEST
        q_util = QuantizationUtility()
        q_util.quantize_module(module)

        x = torch.rand(1, 3, 32, 32)

        # ASSERT
        with torch.no_grad():
            y = module(x)
            torch.testing.assert_close(y.size(), (1, 16, 32, 32))

        self.assertTrue(isinstance(module.conv1, QuantizationUtility.mapping_instructions[nn.Conv2d].quantized_type))
        self.assertTrue(isinstance(module.conv2, nn.Conv2d))

    def test_dynamic_skip_quantization(self):
        # ARRANGE
        class MyModel(nn.Module):
            def __init__(self) -> None:
                super().__init__()
                self.conv1 = nn.Conv2d(3, 8, kernel_size=3, padding=1)
                self.conv2 = nn.Conv2d(8, 16, kernel_size=3, padding=1)

            def forward(self, x):
                return self.conv2(self.conv1(x))

        module = MyModel()

        # TEST
        q_util = QuantizationUtility()
        q_util.register_skip_quantization(layer_names={'conv2'})
        q_util.quantize_module(module)
        x = torch.rand(1, 3, 32, 32)

        # ASSERT
        with torch.no_grad():
            y = module(x)
            torch.testing.assert_close(y.size(), (1, 16, 32, 32))

        self.assertTrue(isinstance(module.conv1, QuantizationUtility.mapping_instructions[nn.Conv2d].quantized_type))
        self.assertTrue(isinstance(module.conv2, nn.Conv2d))

    def test_custom_quantized_mapping_wrapper_explicit_from_float(self):
        # ARRANGE
        class MyBlock(nn.Module):
            def __init__(self, in_feats, out_feats) -> None:
                super().__init__()
                self.flatten = nn.Flatten()
                self.linear = nn.Linear(in_feats, out_feats)

            def forward(self, x):
                return self.linear(self.flatten(x))

        class MyQuantizedBlock(SGQuantMixin):
            # NOTE: **kwargs are necessary because quant descriptors are passed there!
            @classmethod
            def from_float(cls, float_instance: MyBlock, **kwargs):
                return cls(in_feats=float_instance.linear.in_features, out_feats=float_instance.linear.out_features)

            def __init__(self, in_feats, out_feats) -> None:
                super().__init__()
                self.flatten = nn.Flatten()
                self.linear = quant_nn.QuantLinear(in_feats, out_feats)

            def forward(self, x):
                return self.linear(self.flatten(x))

        class MyModel(nn.Module):
            def __init__(self, res, n_classes) -> None:
                super().__init__()
                self.conv = nn.Conv2d(3, 4, kernel_size=3, padding=1)
                self.my_block = QuantizedMapping(float_module=MyBlock(4 * (res ** 2), n_classes),
                                                 quantized_type=MyQuantizedBlock)

            def forward(self, x):
                y = self.conv(x)
                return self.my_block(y)

        res = 32
        n_clss = 10
        module = MyModel(res, n_clss)

        # TEST
        q_util = QuantizationUtility()
        q_util.quantize_module(module)

        x = torch.rand(1, 3, res, res)

        # ASSERT
        with torch.no_grad():
            y = module(x)
            torch.testing.assert_close(y.size(), (1, n_clss))

        self.assertTrue(isinstance(module.conv, QuantizationUtility.mapping_instructions[nn.Conv2d].quantized_type))
        self.assertTrue(isinstance(module.my_block, MyQuantizedBlock))

    def test_custom_quantized_mapping_wrapper_implicit_from_float(self):
        # ARRANGE
        class MyBlock(nn.Module):
            def __init__(self, in_feats, out_feats) -> None:
                super().__init__()
                self.in_feats = in_feats
                self.out_feats = out_feats
                self.flatten = nn.Flatten()
                self.linear = nn.Linear(in_feats, out_feats)

            def forward(self, x):
                return self.linear(self.flatten(x))

        class MyQuantizedBlock(SGQuantMixin):
            # NOTE: **kwargs are necessary because quant descriptors are passed there!
            # NOTE: because we don't override `from_float`,
            #       then the float instance should have `in_feats` and `out_feats` as state
            def __init__(self, in_feats, out_feats, **kwargs) -> None:
                super().__init__()
                self.flatten = nn.Flatten()
                self.linear = quant_nn.QuantLinear(in_feats, out_feats)

            def forward(self, x):
                return self.linear(self.flatten(x))

        class MyModel(nn.Module):
            def __init__(self, res, n_classes) -> None:
                super().__init__()
                self.conv = nn.Conv2d(3, 4, kernel_size=3, padding=1)
                self.my_block = QuantizedMapping(float_module=MyBlock(4 * (res ** 2), n_classes),
                                                 quantized_type=MyQuantizedBlock)

            def forward(self, x):
                y = self.conv(x)
                return self.my_block(y)

        res = 32
        n_clss = 10
        module = MyModel(res, n_clss)

        # TEST
        q_util = QuantizationUtility()
        q_util.quantize_module(module)

        x = torch.rand(1, 3, res, res)

        # ASSERT
        with torch.no_grad():
            y = module(x)
            torch.testing.assert_close(y.size(), (1, n_clss))

        self.assertTrue(isinstance(module.conv, QuantizationUtility.mapping_instructions[nn.Conv2d].quantized_type))
        self.assertTrue(isinstance(module.my_block, MyQuantizedBlock))

    def test_custom_quantized_mapping_register_with_decorator(self):
        # ARRANGE
        class MyBlock(nn.Module):
            def __init__(self, in_feats, out_feats) -> None:
                super().__init__()
                self.in_feats = in_feats
                self.out_feats = out_feats
                self.flatten = nn.Flatten()
                self.linear = nn.Linear(in_feats, out_feats)

            def forward(self, x):
                return self.linear(self.flatten(x))

        @RegisterQuantizedModule(float_module=MyBlock)
        class MyQuantizedBlock(SGQuantMixin):
            # NOTE: **kwargs are necessary because quant descriptors are passed there!
            # NOTE: because we don't override `from_float`,
            #       then the float instance should have `in_feats` and `out_feats` as state
            def __init__(self, in_feats, out_feats, **kwargs) -> None:
                super().__init__()
                self.flatten = nn.Flatten()
                self.linear = quant_nn.QuantLinear(in_feats, out_feats)

            def forward(self, x):
                return self.linear(self.flatten(x))

        class MyModel(nn.Module):
            def __init__(self, res, n_classes) -> None:
                super().__init__()
                self.conv = nn.Conv2d(3, 4, kernel_size=3, padding=1)
                self.my_block = MyBlock(4 * (res ** 2), n_classes)

            def forward(self, x):
                y = self.conv(x)
                return self.my_block(y)

        res = 32
        n_clss = 10
        module = MyModel(res, n_clss)

        # TEST
        q_util = QuantizationUtility()
        q_util.quantize_module(module)

        x = torch.rand(1, 3, res, res)

        # ASSERT
        with torch.no_grad():
            y = module(x)
            torch.testing.assert_close(y.size(), (1, n_clss))
        self.assertTrue(MyQuantizedBlock is not None)
        self.assertTrue(isinstance(module.conv, QuantizationUtility.mapping_instructions[nn.Conv2d].quantized_type))
        self.assertTrue(isinstance(module.my_block, MyQuantizedBlock))

    def test_dynamic_quantized_mapping(self):
        # ARRANGE
        class MyBlock(nn.Module):
            def __init__(self, in_feats, out_feats) -> None:
                super().__init__()
                self.in_feats = in_feats
                self.out_feats = out_feats
                self.flatten = nn.Flatten()
                self.linear = nn.Linear(in_feats, out_feats)

            def forward(self, x):
                return self.linear(self.flatten(x))

        class MyQuantizedBlock(SGQuantMixin):
            # NOTE: **kwargs are necessary because quant descriptors are passed there!
            # NOTE: because we don't override `from_float`,
            #       then the float instance should have `in_feats` and `out_feats` as state
            def __init__(self, in_feats, out_feats, **kwargs) -> None:
                super().__init__()
                self.flatten = nn.Flatten()
                self.linear = quant_nn.QuantLinear(in_feats, out_feats)

            def forward(self, x):
                return self.linear(self.flatten(x))

        class MyModel(nn.Module):
            def __init__(self, res, n_classes) -> None:
                super().__init__()
                self.conv = nn.Conv2d(3, 4, kernel_size=3, padding=1)
                self.my_block = MyBlock(4 * (res ** 2), n_classes)

            def forward(self, x):
                y = self.conv(x)
                return self.my_block(y)

        res = 32
        n_clss = 10
        module = MyModel(res, n_clss)

        # TEST
        q_util = QuantizationUtility()
        q_util.register_quantization_mapping(layer_names={'my_block'}, quantized_type=MyQuantizedBlock)
        q_util.quantize_module(module)

        x = torch.rand(1, 3, res, res)

        # ASSERT
        with torch.no_grad():
            y = module(x)
            torch.testing.assert_close(y.size(), (1, n_clss))

        self.assertTrue(isinstance(module.conv, QuantizationUtility.mapping_instructions[nn.Conv2d].quantized_type))
        self.assertTrue(isinstance(module.my_block, MyQuantizedBlock))


if __name__ == '__main__':
    unittest.main()
