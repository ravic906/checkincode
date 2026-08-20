"""
Problem bank for the SQL practice MVP.

Problems live in Postgres (db.py's `problems` table), not this module --
this file only holds the *content* used to seed that table the first time
the app starts against an empty database, plus the query functions the
rest of the backend calls. Existing problems are never re-seeded once the
table is non-empty, so editing PROBLEMS after first deploy has no effect;
use the admin batch-generation flow (llm.generate_problem_batch) or a
direct SQL insert for anything after the initial seed.

Each problem is self-contained: its own schema, its own seed data, and a
canonical query used both to (a) compute the expected output at problem-load
time and (b) as a reference for the AI explanation prompt.

`order_matters=False` means the grader compares results as multisets (sorted
before comparing) rather than caring about row order -- most problems here
don't ask for a specific order, so getting ORDER BY "wrong" shouldn't fail you.
Problems that explicitly ask for an order (e.g. "top N by X") set it True.

Date-based problems deliberately use a fixed literal reference date instead
of CURRENT_DATE/NOW() -- the expected output is computed once and cached at
startup (see main.py's _EXPECTED_CACHE), so a canonical query tied to "today"
would silently drift out of sync with itself after the day changes.
"""

import json
import uuid

import db
import sandbox
import topics

PROBLEMS = [
    {
        "id": "easy-1-filter-active-employees",
        "title": "Active Employees in Engineering",
        "difficulty": "easy",
        "topic": "Retrieving Records",
        "tags": ["select", "where"],
        "description": (
            "The `employees` table has some rows with a NULL `department` "
            "(not yet assigned) and a couple of duplicate names (rehires). "
            "Return `employee_id`, `full_name`, and `salary` for employees "
            "who are in the 'Engineering' department AND currently active "
            "(`is_active = TRUE`). Do not include NULL departments."
        ),
        "order_matters": False,
        "schema_sql": """
            CREATE TABLE employees (
                employee_id INTEGER,
                full_name VARCHAR,
                department VARCHAR,
                salary INTEGER,
                is_active BOOLEAN
            );
        """,
        "seed_sql": """
            INSERT INTO employees VALUES
            (1, 'Asha Rao', 'Engineering', 95000, TRUE),
            (2, 'Vikram Shah', 'Engineering', 88000, FALSE),
            (3, 'Priya Nair', 'Sales', 62000, TRUE),
            (4, 'Rahul Verma', NULL, 55000, TRUE),
            (5, 'Asha Rao', 'Engineering', 97000, TRUE),
            (6, 'Divya Iyer', 'Engineering', 91000, TRUE),
            (7, 'Karan Mehta', 'Marketing', 60000, TRUE),
            (8, 'Neha Joshi', 'Engineering', NULL, TRUE);
        """,
        "canonical_sql": """
            SELECT employee_id, full_name, salary
            FROM employees
            WHERE department = 'Engineering' AND is_active = TRUE;
        """,
    },
    {
        "id": "easy-2-distinct-customers",
        "title": "Which Customers Have Ordered?",
        "difficulty": "easy",
        "topic": "Retrieving Records",
        "tags": ["select", "distinct"],
        "description": (
            "The `orders` table contains one row per order; a customer can "
            "have many orders (or none, tracked in `customers`). Some "
            "`order_amount` values are NULL (order placed but payment not "
            "captured yet). Return the distinct `customer_id`s that appear "
            "in `orders` at all, regardless of amount, sorted ascending."
        ),
        "order_matters": True,
        "schema_sql": """
            CREATE TABLE orders (
                order_id INTEGER,
                customer_id INTEGER,
                order_amount DECIMAL(10,2)
            );
        """,
        "seed_sql": """
            INSERT INTO orders VALUES
            (101, 1, 250.00),
            (102, 2, NULL),
            (103, 1, 90.50),
            (104, 3, 400.00),
            (105, 2, 75.00),
            (106, 4, NULL);
        """,
        "canonical_sql": """
            SELECT DISTINCT customer_id
            FROM orders
            ORDER BY customer_id;
        """,
    },
    {
        "id": "medium-1-customers-without-orders",
        "title": "Customers Who Never Ordered",
        "difficulty": "medium",
        "topic": "Working with Multiple Tables",
        "tags": ["joins", "left-join", "null-handling"],
        "description": (
            "Using `customers` and `orders`, return the `customer_id` and "
            "`customer_name` of customers who have never placed an order. "
            "Sort by `customer_id` ascending."
        ),
        "order_matters": True,
        "schema_sql": """
            CREATE TABLE customers (
                customer_id INTEGER,
                customer_name VARCHAR
            );
            CREATE TABLE orders (
                order_id INTEGER,
                customer_id INTEGER,
                order_amount DECIMAL(10,2)
            );
        """,
        "seed_sql": """
            INSERT INTO customers VALUES
            (1, 'Ananya Traders'),
            (2, 'Bharat Textiles'),
            (3, 'Chennai Foods'),
            (4, 'Delta Logistics'),
            (5, 'Everest Retail');

            INSERT INTO orders VALUES
            (101, 1, 250.00),
            (102, 3, 400.00),
            (103, 1, 90.50),
            (104, 5, 120.00);
        """,
        "canonical_sql": """
            SELECT c.customer_id, c.customer_name
            FROM customers c
            LEFT JOIN orders o ON c.customer_id = o.customer_id
            WHERE o.order_id IS NULL
            ORDER BY c.customer_id;
        """,
    },
    {
        "id": "medium-2-employee-department-join",
        "title": "Employees With Department Names",
        "difficulty": "medium",
        "topic": "Working with Multiple Tables",
        "tags": ["joins", "inner-join"],
        "description": (
            "The `departments` table accidentally has a duplicate row for "
            "'Engineering' (dept_id 10 and 40, both named 'Engineering' -- "
            "a data entry mistake that happens in real warehouses). Join "
            "`employees` to `departments` on `dept_id` and return "
            "`employee_id`, `full_name`, and `department_name` for every "
            "employee who has a matching department. Watch out for the "
            "duplicate causing extra rows."
        ),
        "order_matters": False,
        "schema_sql": """
            CREATE TABLE departments (
                dept_id INTEGER,
                department_name VARCHAR
            );
            CREATE TABLE employees (
                employee_id INTEGER,
                full_name VARCHAR,
                dept_id INTEGER
            );
        """,
        "seed_sql": """
            INSERT INTO departments VALUES
            (10, 'Engineering'),
            (20, 'Sales'),
            (30, 'Marketing'),
            (40, 'Engineering');

            INSERT INTO employees VALUES
            (1, 'Asha Rao', 10),
            (2, 'Priya Nair', 20),
            (3, 'Karan Mehta', 30),
            (4, 'Rahul Verma', NULL);
        """,
        "canonical_sql": """
            SELECT e.employee_id, e.full_name, d.department_name
            FROM employees e
            INNER JOIN departments d ON e.dept_id = d.dept_id;
        """,
    },
    {
        "id": "medium-3-sales-by-region",
        "title": "Total Sales Per Region",
        "difficulty": "medium",
        "topic": "Reporting and Warehousing",
        "tags": ["aggregation", "group-by", "null-handling"],
        "description": (
            "The `sales` table has a NULL `region` for a couple of rows "
            "(unassigned territory) and NULL `amount` for one row (refund "
            "pending). Return `region` and the total `amount` (as "
            "`total_sales`) per region, excluding NULL regions, treating "
            "NULL amounts as not contributing to the sum. Sort by "
            "`total_sales` descending."
        ),
        "order_matters": True,
        "schema_sql": """
            CREATE TABLE sales (
                sale_id INTEGER,
                region VARCHAR,
                amount DECIMAL(10,2)
            );
        """,
        "seed_sql": """
            INSERT INTO sales VALUES
            (1, 'North', 500.00),
            (2, 'South', 300.00),
            (3, 'North', 200.00),
            (4, NULL, 150.00),
            (5, 'South', NULL),
            (6, 'East', 400.00),
            (7, 'South', 100.00);
        """,
        "canonical_sql": """
            SELECT region, SUM(amount) AS total_sales
            FROM sales
            WHERE region IS NOT NULL
            GROUP BY region
            ORDER BY total_sales DESC;
        """,
    },
    {
        "id": "medium-4-avg-order-having",
        "title": "Customers Averaging Over 200",
        "difficulty": "medium",
        "topic": "Reporting and Warehousing",
        "tags": ["aggregation", "group-by", "having"],
        "description": (
            "Return `customer_id` and their average order amount (as "
            "`avg_order`) from `orders`, but only for customers whose "
            "average order amount is greater than 200. Ignore NULL amounts "
            "when averaging. Sort by `avg_order` descending."
        ),
        "order_matters": True,
        "schema_sql": """
            CREATE TABLE orders (
                order_id INTEGER,
                customer_id INTEGER,
                order_amount DECIMAL(10,2)
            );
        """,
        "seed_sql": """
            INSERT INTO orders VALUES
            (1, 1, 250.00),
            (2, 1, 150.00),
            (3, 2, 400.00),
            (4, 2, NULL),
            (5, 3, 50.00),
            (6, 3, 60.00),
            (7, 4, 900.00);
        """,
        "canonical_sql": """
            SELECT customer_id, AVG(order_amount) AS avg_order
            FROM orders
            GROUP BY customer_id
            HAVING AVG(order_amount) > 200
            ORDER BY avg_order DESC;
        """,
    },
    {
        "id": "hard-1-above-dept-average",
        "title": "Above Their Department's Average Salary",
        "difficulty": "hard",
        "topic": "Advanced Searching",
        "tags": ["subquery", "correlated-subquery"],
        "description": (
            "Return `employee_id`, `full_name`, `department`, and `salary` "
            "for employees who earn more than the average salary of their "
            "own department. NULL salaries should be excluded entirely "
            "(not counted in the average, not returned as a result row). "
            "Sort by `department`, then `salary` descending."
        ),
        "order_matters": True,
        "schema_sql": """
            CREATE TABLE employees (
                employee_id INTEGER,
                full_name VARCHAR,
                department VARCHAR,
                salary INTEGER
            );
        """,
        "seed_sql": """
            INSERT INTO employees VALUES
            (1, 'Asha Rao', 'Engineering', 95000),
            (2, 'Vikram Shah', 'Engineering', 80000),
            (3, 'Divya Iyer', 'Engineering', 91000),
            (4, 'Priya Nair', 'Sales', 62000),
            (5, 'Karan Mehta', 'Sales', 70000),
            (6, 'Neha Joshi', 'Sales', NULL),
            (7, 'Rahul Verma', 'Sales', 58000);
        """,
        "canonical_sql": """
            SELECT employee_id, full_name, department, salary
            FROM employees e
            WHERE salary IS NOT NULL
              AND salary > (
                  SELECT AVG(salary)
                  FROM employees e2
                  WHERE e2.department = e.department AND e2.salary IS NOT NULL
              )
            ORDER BY department, salary DESC;
        """,
    },
    {
        "id": "hard-2-customers-all-products-in-category",
        "title": "Customers Who Bought Every 'Snacks' Product",
        "difficulty": "hard",
        "topic": "Advanced Searching",
        "tags": ["subquery", "not-exists", "set-based"],
        "description": (
            "There are 3 products in the 'Snacks' category. Return the "
            "`customer_id` of every customer who has purchased ALL of them "
            "(at least once each) -- a classic relational-division problem. "
            "A customer may also buy other, non-Snacks products; that "
            "shouldn't disqualify them. Sort by `customer_id`."
        ),
        "order_matters": True,
        "schema_sql": """
            CREATE TABLE products (
                product_id INTEGER,
                product_name VARCHAR,
                category VARCHAR
            );
            CREATE TABLE purchases (
                purchase_id INTEGER,
                customer_id INTEGER,
                product_id INTEGER
            );
        """,
        "seed_sql": """
            INSERT INTO products VALUES
            (1, 'Chips', 'Snacks'),
            (2, 'Cookies', 'Snacks'),
            (3, 'Namkeen', 'Snacks'),
            (4, 'Soda', 'Beverages');

            INSERT INTO purchases VALUES
            (1, 100, 1),
            (2, 100, 2),
            (3, 100, 3),
            (4, 100, 4),
            (5, 200, 1),
            (6, 200, 2),
            (7, 300, 1),
            (8, 300, 2),
            (9, 300, 3);
        """,
        "canonical_sql": """
            SELECT p.customer_id
            FROM purchases p
            JOIN products pr ON p.product_id = pr.product_id
            WHERE pr.category = 'Snacks'
            GROUP BY p.customer_id
            HAVING COUNT(DISTINCT pr.product_id) = (
                SELECT COUNT(*) FROM products WHERE category = 'Snacks'
            )
            ORDER BY p.customer_id;
        """,
    },
    {
        "id": "hard-3-rank-salary-in-department",
        "title": "Rank Employees by Salary Within Department",
        "difficulty": "hard",
        "topic": "Reporting and Warehousing",
        "tags": ["window-functions", "rank"],
        "description": (
            "Two employees in Sales are tied on salary. Using a window "
            "function, return `employee_id`, `full_name`, `department`, "
            "`salary`, and a `salary_rank` column that ranks employees by "
            "salary (highest = 1) within each department, using standard "
            "competition ranking (ties share a rank, next rank skips -- "
            "i.e. RANK(), not ROW_NUMBER() or DENSE_RANK()). Sort by "
            "`department`, then `salary_rank`."
        ),
        "order_matters": True,
        "schema_sql": """
            CREATE TABLE employees (
                employee_id INTEGER,
                full_name VARCHAR,
                department VARCHAR,
                salary INTEGER
            );
        """,
        "seed_sql": """
            INSERT INTO employees VALUES
            (1, 'Asha Rao', 'Engineering', 95000),
            (2, 'Vikram Shah', 'Engineering', 88000),
            (3, 'Divya Iyer', 'Engineering', 91000),
            (4, 'Priya Nair', 'Sales', 70000),
            (5, 'Karan Mehta', 'Sales', 70000),
            (6, 'Rahul Verma', 'Sales', 58000);
        """,
        "canonical_sql": """
            SELECT
                employee_id,
                full_name,
                department,
                salary,
                RANK() OVER (PARTITION BY department ORDER BY salary DESC) AS salary_rank
            FROM employees
            ORDER BY department, salary_rank;
        """,
    },
    {
        "id": "hard-4-running-total-sales",
        "title": "Running Total of Daily Sales",
        "difficulty": "hard",
        "topic": "Reporting and Warehousing",
        "tags": ["window-functions", "running-total"],
        "description": (
            "The `daily_sales` table has one row per date (note: two rows "
            "share the same date, 2024-01-03, from a duplicate batch load "
            "-- both should still be included). Return `sale_date`, "
            "`amount`, and a `running_total` column showing the cumulative "
            "sum of `amount` ordered by `sale_date` (and by `sale_id` to "
            "break ties on the same date). Sort by `sale_date`, then "
            "`sale_id`."
        ),
        "order_matters": True,
        "schema_sql": """
            CREATE TABLE daily_sales (
                sale_id INTEGER,
                sale_date DATE,
                amount DECIMAL(10,2)
            );
        """,
        "seed_sql": """
            INSERT INTO daily_sales VALUES
            (1, '2024-01-01', 100.00),
            (2, '2024-01-02', 150.00),
            (3, '2024-01-03', 200.00),
            (4, '2024-01-03', 50.00),
            (5, '2024-01-04', 300.00);
        """,
        "canonical_sql": """
            SELECT
                sale_date,
                amount,
                SUM(amount) OVER (ORDER BY sale_date, sale_id
                                  ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS running_total
            FROM daily_sales
            ORDER BY sale_date, sale_id;
        """,
    },

    # ---- New problems: Sorting Query Results ----
    {
        "id": "easy-3-sort-employees-by-salary",
        "title": "Employees Sorted by Salary, Nulls Last",
        "difficulty": "easy",
        "topic": "Sorting Query Results",
        "tags": ["sorting", "order-by", "null-handling"],
        "description": (
            "Some employees don't have a `salary` recorded yet (NULL). "
            "Return `employee_id`, `full_name`, and `salary` for all "
            "employees, sorted by `salary` descending -- but NULL salaries "
            "should sort LAST regardless of direction, not first (DuckDB's "
            "default NULL ordering may not already do what you want here)."
        ),
        "order_matters": True,
        "schema_sql": """
            CREATE TABLE employees (
                employee_id INTEGER,
                full_name VARCHAR,
                salary INTEGER
            );
        """,
        "seed_sql": """
            INSERT INTO employees VALUES
            (1, 'Asha Rao', 95000),
            (2, 'Vikram Shah', NULL),
            (3, 'Divya Iyer', 91000),
            (4, 'Priya Nair', 62000),
            (5, 'Karan Mehta', NULL);
        """,
        "canonical_sql": """
            SELECT employee_id, full_name, salary
            FROM employees
            ORDER BY salary DESC NULLS LAST;
        """,
    },
    {
        "id": "medium-5-sort-multi-column",
        "title": "Sort by Department, Then Salary",
        "difficulty": "medium",
        "topic": "Sorting Query Results",
        "tags": ["sorting", "order-by", "multi-column"],
        "description": (
            "Return `employee_id`, `full_name`, `department`, and `salary` "
            "for all employees, sorted by `department` ascending, and "
            "within each department by `salary` descending."
        ),
        "order_matters": True,
        "schema_sql": """
            CREATE TABLE employees (
                employee_id INTEGER,
                full_name VARCHAR,
                department VARCHAR,
                salary INTEGER
            );
        """,
        "seed_sql": """
            INSERT INTO employees VALUES
            (1, 'Asha Rao', 'Engineering', 95000),
            (2, 'Vikram Shah', 'Engineering', 88000),
            (3, 'Priya Nair', 'Sales', 62000),
            (4, 'Karan Mehta', 'Sales', 70000),
            (5, 'Divya Iyer', 'Engineering', 91000);
        """,
        "canonical_sql": """
            SELECT employee_id, full_name, department, salary
            FROM employees
            ORDER BY department ASC, salary DESC;
        """,
    },

    # ---- New problems: Metadata Queries ----
    {
        "id": "medium-6-metadata-column-types",
        "title": "List Columns and Types of `orders`",
        "difficulty": "medium",
        "topic": "Metadata Queries",
        "tags": ["metadata", "information-schema"],
        "description": (
            "Query the database's own catalog (not the `orders` data "
            "itself) to return the `column_name` and `data_type` of every "
            "column in the `orders` table, in their declared order. "
            "`information_schema.columns` is a standard way to do this "
            "across most SQL databases."
        ),
        "order_matters": True,
        "schema_sql": """
            CREATE TABLE orders (
                order_id INTEGER,
                customer_id INTEGER,
                order_date DATE,
                total_amount DECIMAL(10,2)
            );
        """,
        "seed_sql": """
            INSERT INTO orders VALUES
            (1, 101, '2024-01-05', 250.00);
        """,
        "canonical_sql": """
            SELECT column_name, data_type
            FROM information_schema.columns
            WHERE table_name = 'orders'
            ORDER BY ordinal_position;
        """,
    },
    {
        "id": "medium-7-metadata-not-null-columns",
        "title": "Find Non-Nullable Columns",
        "difficulty": "medium",
        "topic": "Metadata Queries",
        "tags": ["metadata", "information-schema"],
        "description": (
            "The `products` table declares some columns as NOT NULL. "
            "Query `information_schema.columns` to return the "
            "`column_name` of every column in `products` that does NOT "
            "allow NULL values, ordered by their declared column order."
        ),
        "order_matters": True,
        "schema_sql": """
            CREATE TABLE products (
                product_id INTEGER NOT NULL,
                product_name VARCHAR NOT NULL,
                description VARCHAR,
                price DECIMAL(10,2)
            );
        """,
        "seed_sql": """
            INSERT INTO products VALUES
            (1, 'Chips', 'Salted potato chips', 40.00);
        """,
        "canonical_sql": """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_name = 'products' AND is_nullable = 'NO'
            ORDER BY ordinal_position;
        """,
    },

    # ---- New problems: Working with Strings ----
    {
        "id": "easy-4-email-domains",
        "title": "Extract Customer Email Domains",
        "difficulty": "easy",
        "topic": "Working with Strings",
        "tags": ["strings", "split-part"],
        "description": (
            "Return `customer_id` and the domain portion of each "
            "customer's `email` (everything after the `@`) as `domain`."
        ),
        "order_matters": False,
        "schema_sql": """
            CREATE TABLE customers (
                customer_id INTEGER,
                email VARCHAR
            );
        """,
        "seed_sql": """
            INSERT INTO customers VALUES
            (1, 'ananya@gmail.com'),
            (2, 'ravi.k@outlook.com'),
            (3, 'sales@bharattextiles.in');
        """,
        "canonical_sql": """
            SELECT customer_id, split_part(email, '@', 2) AS domain
            FROM customers;
        """,
    },
    {
        "id": "medium-8-first-word-of-product",
        "title": "First Word of Multi-Word Product Names",
        "difficulty": "medium",
        "topic": "Working with Strings",
        "tags": ["strings", "like", "split-part"],
        "description": (
            "Some `product_name` values are a single word, others are "
            "multiple words separated by spaces. Return `product_id` and "
            "the first word of `product_name` as `first_word`, but ONLY "
            "for products whose name actually contains more than one word."
        ),
        "order_matters": False,
        "schema_sql": """
            CREATE TABLE products (
                product_id INTEGER,
                product_name VARCHAR
            );
        """,
        "seed_sql": """
            INSERT INTO products VALUES
            (1, 'Chips'),
            (2, 'Masala Chips'),
            (3, 'Soda'),
            (4, 'Diet Cola Soda');
        """,
        "canonical_sql": """
            SELECT product_id, split_part(product_name, ' ', 1) AS first_word
            FROM products
            WHERE product_name LIKE '% %';
        """,
    },

    # ---- New problems: Working with Numbers ----
    {
        "id": "easy-5-discounted-price",
        "title": "Round Prices and Compute a Discount",
        "difficulty": "easy",
        "topic": "Working with Numbers",
        "tags": ["numbers", "round", "arithmetic"],
        "description": (
            "Return `product_id`, `price` rounded to 2 decimal places, and "
            "a `discounted_price` column that's 10% off `price`, also "
            "rounded to 2 decimal places."
        ),
        "order_matters": False,
        "schema_sql": """
            CREATE TABLE products (
                product_id INTEGER,
                price DECIMAL(10,4)
            );
        """,
        "seed_sql": """
            INSERT INTO products VALUES
            (1, 199.995),
            (2, 45.111),
            (3, 1000.006);
        """,
        "canonical_sql": """
            SELECT product_id, ROUND(price, 2) AS price, ROUND(price * 0.9, 2) AS discounted_price
            FROM products;
        """,
    },
    {
        "id": "medium-9-round-hundred-orders",
        "title": "Flag Orders That Are Round Hundreds",
        "difficulty": "medium",
        "topic": "Working with Numbers",
        "tags": ["numbers", "modulo", "case"],
        "description": (
            "Return `order_id`, `total_amount`, and a `category` column "
            "that says 'Round Hundred' if `total_amount` is exactly "
            "divisible by 100, otherwise 'Other'."
        ),
        "order_matters": False,
        "schema_sql": """
            CREATE TABLE orders (
                order_id INTEGER,
                total_amount INTEGER
            );
        """,
        "seed_sql": """
            INSERT INTO orders VALUES
            (1, 500),
            (2, 450),
            (3, 1200),
            (4, 275);
        """,
        "canonical_sql": """
            SELECT order_id, total_amount,
                   CASE WHEN MOD(total_amount, 100) = 0 THEN 'Round Hundred' ELSE 'Other' END AS category
            FROM orders;
        """,
    },

    # ---- New problems: Date Arithmetic ----
    {
        "id": "easy-6-orders-in-date-window",
        "title": "Orders Placed in a 30-Day Window",
        "difficulty": "easy",
        "topic": "Date Arithmetic",
        "tags": ["dates", "between"],
        "description": (
            "Treat 2024-07-15 as \"today\" for this problem (don't use "
            "CURRENT_DATE). Return `order_id` and `order_date` for orders "
            "placed in the 30 days up to and including 2024-07-15, i.e. "
            "from 2024-06-15 onward."
        ),
        "order_matters": False,
        "schema_sql": """
            CREATE TABLE orders (
                order_id INTEGER,
                order_date DATE
            );
        """,
        "seed_sql": """
            INSERT INTO orders VALUES
            (1, '2024-05-01'),
            (2, '2024-06-20'),
            (3, '2024-07-01'),
            (4, '2024-07-15'),
            (5, '2024-08-01');
        """,
        "canonical_sql": """
            SELECT order_id, order_date
            FROM orders
            WHERE order_date BETWEEN DATE '2024-06-15' AND DATE '2024-07-15';
        """,
    },
    {
        "id": "medium-10-days-to-ship",
        "title": "Days Between Order and Shipment",
        "difficulty": "medium",
        "topic": "Date Arithmetic",
        "tags": ["dates", "arithmetic"],
        "description": (
            "Some orders haven't shipped yet (`ship_date` is NULL). Return "
            "`order_id` and a `days_to_ship` column: the number of days "
            "between `order_date` and `ship_date`. Leave `days_to_ship` as "
            "NULL for orders that haven't shipped."
        ),
        "order_matters": False,
        "schema_sql": """
            CREATE TABLE orders (
                order_id INTEGER,
                order_date DATE,
                ship_date DATE
            );
        """,
        "seed_sql": """
            INSERT INTO orders VALUES
            (1, '2024-06-01', '2024-06-04'),
            (2, '2024-06-10', '2024-06-10'),
            (3, '2024-06-15', NULL),
            (4, '2024-06-20', '2024-06-25');
        """,
        "canonical_sql": """
            SELECT order_id, (ship_date - order_date) AS days_to_ship
            FROM orders;
        """,
    },

    # ---- New problems: Date Manipulation ----
    {
        "id": "easy-7-truncate-to-month",
        "title": "Bucket Orders by Month",
        "difficulty": "easy",
        "topic": "Date Manipulation",
        "tags": ["dates", "date-trunc"],
        "description": (
            "Return `order_id` and an `order_month` column: `order_date` "
            "truncated to the first day of its month."
        ),
        "order_matters": False,
        "schema_sql": """
            CREATE TABLE orders (
                order_id INTEGER,
                order_date DATE
            );
        """,
        "seed_sql": """
            INSERT INTO orders VALUES
            (1, '2024-06-05'),
            (2, '2024-06-27'),
            (3, '2024-07-02');
        """,
        "canonical_sql": """
            SELECT order_id, DATE_TRUNC('month', order_date) AS order_month
            FROM orders;
        """,
    },
    {
        "id": "medium-11-return-deadline",
        "title": "Compute a Return Deadline",
        "difficulty": "medium",
        "topic": "Date Manipulation",
        "tags": ["dates", "interval"],
        "description": (
            "Orders can be returned within 7 days of shipping. Return "
            "`order_id` and a `return_deadline` column: `ship_date` plus 7 "
            "days. Orders with a NULL `ship_date` should have a NULL "
            "`return_deadline`."
        ),
        "order_matters": False,
        "schema_sql": """
            CREATE TABLE orders (
                order_id INTEGER,
                ship_date DATE
            );
        """,
        "seed_sql": """
            INSERT INTO orders VALUES
            (1, '2024-06-01'),
            (2, '2024-06-15'),
            (3, NULL);
        """,
        "canonical_sql": """
            SELECT order_id, ship_date + INTERVAL 7 DAY AS return_deadline
            FROM orders;
        """,
    },

    # ---- New problems: Working with Ranges ----
    {
        "id": "easy-8-salary-bands",
        "title": "Bucket Employees Into Salary Bands",
        "difficulty": "easy",
        "topic": "Working with Ranges",
        "tags": ["ranges", "case", "between"],
        "description": (
            "Return `employee_id`, `salary`, and a `band` column: "
            "'Low' for salary under 50000, 'Mid' for 50000 to 90000 "
            "inclusive, and 'High' for anything above 90000."
        ),
        "order_matters": False,
        "schema_sql": """
            CREATE TABLE employees (
                employee_id INTEGER,
                salary INTEGER
            );
        """,
        "seed_sql": """
            INSERT INTO employees VALUES
            (1, 45000),
            (2, 62000),
            (3, 90000),
            (4, 120000),
            (5, 55000);
        """,
        "canonical_sql": """
            SELECT employee_id, salary,
                   CASE
                       WHEN salary < 50000 THEN 'Low'
                       WHEN salary BETWEEN 50000 AND 90000 THEN 'Mid'
                       ELSE 'High'
                   END AS band
            FROM employees;
        """,
    },
    {
        "id": "medium-12-overlapping-bookings",
        "title": "Find Overlapping Room Bookings",
        "difficulty": "medium",
        "topic": "Working with Ranges",
        "tags": ["ranges", "date-overlap"],
        "description": (
            "Return the `booking_id` of every booking for `room_id` 101 "
            "whose date range overlaps at all with 2024-07-01 through "
            "2024-07-10 inclusive (a booking overlaps if it starts on or "
            "before the window ends, AND ends on or after the window "
            "starts)."
        ),
        "order_matters": True,
        "schema_sql": """
            CREATE TABLE bookings (
                booking_id INTEGER,
                room_id INTEGER,
                start_date DATE,
                end_date DATE
            );
        """,
        "seed_sql": """
            INSERT INTO bookings VALUES
            (1, 101, '2024-06-25', '2024-06-30'),
            (2, 101, '2024-07-05', '2024-07-08'),
            (3, 101, '2024-07-09', '2024-07-15'),
            (4, 101, '2024-07-11', '2024-07-20'),
            (5, 102, '2024-07-05', '2024-07-08');
        """,
        "canonical_sql": """
            SELECT booking_id
            FROM bookings
            WHERE room_id = 101
              AND start_date <= DATE '2024-07-10'
              AND end_date >= DATE '2024-07-01'
            ORDER BY booking_id;
        """,
    },

    # ---- New problems: Hierarchical Queries ----
    {
        "id": "medium-13-employee-manager-name",
        "title": "Employees With Their Manager's Name",
        "difficulty": "medium",
        "topic": "Hierarchical Queries",
        "tags": ["self-join", "hierarchy"],
        "description": (
            "The `employees` table is self-referential: `manager_id` "
            "points to another row's `employee_id` (NULL for the CEO, who "
            "has no manager). Return `employee_id`, `name`, and "
            "`manager_name` (NULL if they have no manager)."
        ),
        "order_matters": False,
        "schema_sql": """
            CREATE TABLE employees (
                employee_id INTEGER,
                name VARCHAR,
                manager_id INTEGER
            );
        """,
        "seed_sql": """
            INSERT INTO employees VALUES
            (1, 'Meera CEO', NULL),
            (2, 'Asha Rao', 1),
            (3, 'Vikram Shah', 1),
            (4, 'Divya Iyer', 2),
            (5, 'Karan Mehta', 2);
        """,
        "canonical_sql": """
            SELECT e.employee_id, e.name, m.name AS manager_name
            FROM employees e
            LEFT JOIN employees m ON e.manager_id = m.employee_id;
        """,
    },
    {
        "id": "hard-5-all-reports-under-manager",
        "title": "Every Employee Under a Manager, Any Depth",
        "difficulty": "hard",
        "topic": "Hierarchical Queries",
        "tags": ["recursive-cte", "hierarchy"],
        "description": (
            "Using a recursive CTE, return every employee (at any depth) "
            "who reports up to employee_id 1 (Meera, the CEO), whether "
            "directly or through a chain of managers. Return "
            "`employee_id`, `name`, and `level` (1 for direct reports of "
            "the CEO, 2 for their reports, and so on). Sort by `level`, "
            "then `employee_id`."
        ),
        "order_matters": True,
        "schema_sql": """
            CREATE TABLE employees (
                employee_id INTEGER,
                name VARCHAR,
                manager_id INTEGER
            );
        """,
        "seed_sql": """
            INSERT INTO employees VALUES
            (1, 'Meera CEO', NULL),
            (2, 'Asha Rao', 1),
            (3, 'Vikram Shah', 1),
            (4, 'Divya Iyer', 2),
            (5, 'Karan Mehta', 2),
            (6, 'Priya Nair', 4);
        """,
        "canonical_sql": """
            WITH RECURSIVE subordinates AS (
                SELECT employee_id, name, manager_id, 1 AS level
                FROM employees
                WHERE manager_id = 1
                UNION ALL
                SELECT e.employee_id, e.name, e.manager_id, s.level + 1
                FROM employees e
                JOIN subordinates s ON e.manager_id = s.employee_id
            )
            SELECT employee_id, name, level
            FROM subordinates
            ORDER BY level, employee_id;
        """,
    },

    # ---- New problems: Odds and Ends ----
    {
        "id": "easy-9-distinct-customer-product-pairs",
        "title": "Distinct Customer-Product Purchase Pairs",
        "difficulty": "easy",
        "topic": "Odds and Ends",
        "tags": ["distinct", "duplicates"],
        "description": (
            "The `purchases` table has duplicate rows where the same "
            "customer bought the same product more than once (logged as "
            "separate rows). Return the distinct `(customer_id, "
            "product_id)` pairs, one row per pair no matter how many times "
            "it was actually purchased."
        ),
        "order_matters": False,
        "schema_sql": """
            CREATE TABLE purchases (
                purchase_id INTEGER,
                customer_id INTEGER,
                product_id INTEGER
            );
        """,
        "seed_sql": """
            INSERT INTO purchases VALUES
            (1, 100, 1),
            (2, 100, 1),
            (3, 100, 2),
            (4, 200, 1),
            (5, 200, 1),
            (6, 200, 1);
        """,
        "canonical_sql": """
            SELECT DISTINCT customer_id, product_id
            FROM purchases;
        """,
    },
    {
        "id": "medium-14-pivot-quarterly-sales",
        "title": "Pivot Sales Into Quarter Columns",
        "difficulty": "medium",
        "topic": "Odds and Ends",
        "tags": ["pivot", "conditional-aggregation"],
        "description": (
            "The `sales` table has one row per (region, quarter, amount). "
            "Return one row per `region` with two columns, `q1_total` and "
            "`q2_total`, summing `amount` for 'Q1' and 'Q2' respectively "
            "(0 if a region had no sales in that quarter). Sort by "
            "`region`."
        ),
        "order_matters": True,
        "schema_sql": """
            CREATE TABLE sales (
                sale_id INTEGER,
                region VARCHAR,
                quarter VARCHAR,
                amount DECIMAL(10,2)
            );
        """,
        "seed_sql": """
            INSERT INTO sales VALUES
            (1, 'North', 'Q1', 500.00),
            (2, 'North', 'Q2', 300.00),
            (3, 'South', 'Q1', 200.00),
            (4, 'East', 'Q2', 400.00);
        """,
        "canonical_sql": """
            SELECT region,
                   SUM(CASE WHEN quarter = 'Q1' THEN amount ELSE 0 END) AS q1_total,
                   SUM(CASE WHEN quarter = 'Q2' THEN amount ELSE 0 END) AS q2_total
            FROM sales
            GROUP BY region
            ORDER BY region;
        """,
    },
]


def seed_if_empty():
    """One-time migration: populate the `problems` table from PROBLEMS
    above if the table is empty. No-op on every subsequent startup."""
    with db.get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM problems")
            (count,) = cur.fetchone()
            if count > 0:
                return
            for p in PROBLEMS:
                cur.execute(
                    """
                    INSERT INTO problems
                        (id, title, difficulty, topic, tags, description,
                         schema_sql, seed_sql, canonical_sql, order_matters, status)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'live')
                    """,
                    (
                        p["id"], p["title"], p["difficulty"], p["topic"],
                        json.dumps(p["tags"]), p["description"].strip(),
                        p["schema_sql"], p["seed_sql"], p["canonical_sql"],
                        p["order_matters"],
                    ),
                )


def get_problem(problem_id: str):
    with db.get_conn() as conn:
        with db.dict_cursor(conn) as cur:
            cur.execute("SELECT * FROM problems WHERE id = %s AND status = 'live'", (problem_id,))
            row = cur.fetchone()
    return dict(row) if row else None


def list_all_live_problems():
    """Used by main.py's startup cache-warming -- every live problem,
    full content (needed to run canonical_sql)."""
    with db.get_conn() as conn:
        with db.dict_cursor(conn) as cur:
            cur.execute("SELECT * FROM problems WHERE status = 'live'")
            rows = cur.fetchall()
    return [dict(r) for r in rows]


def list_problems_summary(difficulty: str | None = None, tag: str | None = None, topic: str | None = None):
    query = "SELECT id, title, difficulty, topic, tags FROM problems WHERE status = 'live'"
    params = []
    if difficulty:
        query += " AND difficulty = %s"
        params.append(difficulty)
    if topic:
        query += " AND topic = %s"
        params.append(topic)
    query += " ORDER BY id"

    with db.get_conn() as conn:
        with db.dict_cursor(conn) as cur:
            cur.execute(query, params)
            rows = [dict(r) for r in cur.fetchall()]

    if tag:
        rows = [r for r in rows if tag in r["tags"]]
    return rows


class InvalidDraftProblem(Exception):
    pass


def insert_pending_draft(draft: dict) -> str:
    """
    Validates and stores one LLM-drafted problem as status='pending_review'
    -- never 'live' directly, a human always approves first. Raises
    InvalidDraftProblem (caller should skip the draft, not fail the whole
    batch) if it's missing fields, targets a non-gradeable topic (i.e.
    DML), or its canonical_sql doesn't pass the same read-only validation
    every student submission goes through -- this is a code-level check
    independent of whether the model actually followed the prompt.
    """
    required = ["title", "difficulty", "topic", "tags", "description", "schema_sql", "seed_sql", "canonical_sql", "order_matters"]
    missing = [f for f in required if f not in draft]
    if missing:
        raise InvalidDraftProblem(f"Draft missing fields: {missing}")

    if draft["topic"] not in topics.GRADEABLE_TOPICS:
        raise InvalidDraftProblem(f"Draft topic '{draft['topic']}' is not a gradeable topic (DML problems aren't supported).")

    try:
        sandbox.validate_student_sql(draft["canonical_sql"])
    except sandbox.SqlValidationError as e:
        raise InvalidDraftProblem(f"canonical_sql failed read-only validation: {e}")

    # Also confirm it actually runs cleanly against its own schema/seed --
    # catches drafts that are read-only-valid syntactically but broken.
    try:
        sandbox.compute_expected_output({
            "schema_sql": draft["schema_sql"],
            "seed_sql": draft["seed_sql"],
            "canonical_sql": draft["canonical_sql"],
        })
    except Exception as e:
        raise InvalidDraftProblem(f"canonical_sql failed to execute: {e}")

    problem_id = f"generated-{uuid.uuid4().hex[:8]}"
    with db.get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO problems
                    (id, title, difficulty, topic, tags, description,
                     schema_sql, seed_sql, canonical_sql, order_matters, status)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'pending_review')
                """,
                (
                    problem_id, draft["title"], draft["difficulty"], draft["topic"],
                    json.dumps(draft["tags"]), draft["description"].strip(),
                    draft["schema_sql"], draft["seed_sql"], draft["canonical_sql"],
                    bool(draft["order_matters"]),
                ),
            )
    return problem_id


def list_pending_problems():
    with db.get_conn() as conn:
        with db.dict_cursor(conn) as cur:
            cur.execute("SELECT * FROM problems WHERE status = 'pending_review' ORDER BY created_at")
            return [dict(r) for r in cur.fetchall()]


def approve_problem(problem_id: str) -> bool:
    with db.get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE problems SET status = 'live' WHERE id = %s AND status = 'pending_review'", (problem_id,))
            return cur.rowcount > 0


def reject_problem(problem_id: str) -> bool:
    with db.get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM problems WHERE id = %s AND status = 'pending_review'", (problem_id,))
            return cur.rowcount > 0


def get_last_batch_generated_at():
    with db.get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT last_batch_generated_at FROM content_cadence WHERE id = 1")
            row = cur.fetchone()
            return row[0] if row else None


def mark_batch_generated():
    with db.get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE content_cadence SET last_batch_generated_at = now() WHERE id = 1")
