import os
import re
import warnings

import numpy as np
import pandas as pd
import scipy
from pandas import DataFrame
from sklearn.feature_extraction.text import TfidfTransformer
from sklearn.neighbors import BallTree, KDTree, NearestNeighbors
from sklearn.preprocessing import MultiLabelBinarizer, Normalizer
from tqdm import tqdm


class BaseRecommender(object):
    def __init__(self, items_path: str, train_path: str, test_path: str, val_path: str) -> None:
        """Base recommender class

        Args:
            items_path (str): Path to pickle file containing the items
            train_path (str): Path to train data parquet file
            test_path (str): Path to test data parquet file
            val_path (str): Path to validation data parquet file
        """
        items = self._preprocess_items(pd.read_pickle(items_path))
        self.items = self._generate_item_features(items)
        self.train = pd.read_parquet(train_path)
        self.test = pd.read_parquet(test_path)
        self.val = pd.read_parquet(val_path)
        self.recommendations = DataFrame()
        
    def _preprocess_items(self, items: DataFrame) -> DataFrame:
        """Applies preprocessing to the items

        Args:
            items (DataFrame): Dataframe containing all items with their metadata

        Returns:
            DataFrame: Sanitised item metadata
        """
        items["price"] = items["price"].apply(lambda p: np.float32(p) if re.match(r"\d+(?:.\d{2})?", str(p)) else 0)
        items["metascore"] = items["metascore"].apply(lambda m: m if m != "NA" else np.nan)
        items["developer"].fillna(value='', inplace=True)
        items["developer"] = items["developer"].apply(lambda my_str: my_str.lower().split(','))
        items["publisher"].fillna(value='', inplace=True)
        items["publisher"] = items["publisher"].apply(lambda my_str: my_str.lower().split(','))
        items["early_access"] = items["early_access"].apply(lambda x: ["earlyaccess"] if x else [])
        items["specs"] = items["specs"].apply(lambda l: [re.subn(r"[^a-z0-9]", "", my_str.lower())[0] for my_str in l])
        items["tags"] = items["tags"].apply(lambda l: [re.subn(r"[^a-z0-9]", "", my_str.lower())[0] for my_str in l])
        items["genres"] = items["genres"].apply(lambda l: [re.subn(r"[^a-z0-9]", "", my_str.lower())[0] for my_str in l])
        return items
    
    def set_user_data(self, train_path: str, test_path: str, val_path: str) -> None:
        """Read new train, test and val data

        Args:
            train_path (str): Path to train parquet file
            test_path (str): Path to test parquet file
            val_path (str): Path to validation parquet file
        """
        self.train = pd.read_parquet(train_path)
        self.test = pd.read_parquet(test_path)
        self.val = pd.read_parquet(val_path)
    
    def _generate_item_features(self, items: DataFrame):
        """Generates the item representations

        Args:
            items (DataFrame): Dataframe containing only relevant metadata
        """
        pass

    def evaluate(self, filename=None, qual_eval_folder=None, k=10, val=False) -> dict:
        """Evaluate the recommendations

        Args:
            filename (str, optional): filename for qualitative evaluation. Defaults to None.
            qual_eval_folder (str, optional): output folder for qualitative evaluation. Defaults to None.
            k (int, optional): Amount of recommendations to consider. Defaults to 10.
            val (bool, optional): Wether or not to use test or validation dataset. Defaults to False.

        Returns:
            dict: a dict containing the hitrate@k, recall@k and nDCG@k
        """
        
        gt = self.val if val else self.test
        gt.rename(columns={"item_id": "items"}, inplace=True)
            
        eval = self.recommendations
        eval = eval.merge(gt, left_index=True, right_index=True)

        results_dict = dict()

        if filename and qual_eval_folder:
            if not os.path.exists(qual_eval_folder):
                os.makedirs(qual_eval_folder)
            eval.to_parquet(qual_eval_folder + filename + '.parquet')
            
        # Cap to k recommendations
        eval['recommendations'] = eval['recommendations'].apply(lambda rec: rec[:k])
        
        # compute HR@k
        eval['HR@k'] = eval.apply(lambda row: int(any(item in row['recommendations'] for item in row['items'])), axis=1)
        results_dict[f'HR@{k}'] = eval['HR@k'].mean()

        # compute nDCG@k
        eval['nDCG@k'] = eval.apply(lambda row: np.sum([int(rec in row['items'])/(np.log2(i+2)) for i, rec in enumerate(row['recommendations'])]), axis=1)
        eval['nDCG@k'] = eval.apply(lambda row: row['nDCG@k']/np.sum([1/(np.log2(i+2)) for i in range(min(len(row['recommendations']), len(row['items'])))]), axis=1)
        results_dict[f'nDCG@{k}'] = eval['nDCG@k'].mean()

        # compute recall@k
        eval['items'] = eval['items'].apply(set)
        eval['recommendations'] = eval['recommendations'].apply(set)
        eval['recall@k'] = eval.apply(lambda row: len(row['recommendations'].intersection(row['items']))/len(row['items']), axis=1)
        results_dict[f'recall@{k}'] = eval['recall@k'].mean()

        # compute ideal recall@k
        eval['ideal_recall@k'] = eval.apply(lambda row: min(len(row['items']), len(row["recommendations"]))/len(row['items']), axis=1)
        results_dict[f'ideal_recall@{k}'] = eval['ideal_recall@k'].mean()
        
        # compute normalised recall@k
        eval['nRecall@k'] = eval.apply(lambda row: row['recall@k']/row['ideal_recall@k'], axis=1)
        results_dict[f'nRecall@{k}'] = eval['nRecall@k'].mean()

        return results_dict
    
    
class ContentBasedRecommender(BaseRecommender):
    def __init__(self, items_path: str, train_path: str, test_path: str, val_path: str, sparse: bool = True, tfidf='default', normalize=False) -> None:
        """Content based recommender

        Args:
            items_path (str): Path to pickle file containing the items
            train_path (str): Path to train data parquet file
            test_path (str): Path to test data parquet file
            val_path (str): Path to validation data parquet file
            sparse (bool, optional): If sparse representation should be used. Defaults to True.
            tfidf (str, optional): Which tf-idf method to use. Defaults to 'default'.
            normalize (bool, optional): If normalization should be used. Defaults to False.
        """
        
        self.sparse = sparse
        self.normalize = normalize
        self.recommendations = None
        self.normalizer = Normalizer(copy=False)
        
        # Select tf-idf method to use
        self.tfidf = None
        if tfidf == 'default':
            self.tfidf = TfidfTransformer(smooth_idf=False, sublinear_tf=False)
        elif tfidf == 'smooth':
            self.tfidf = TfidfTransformer(smooth_idf=True, sublinear_tf=False)
        elif tfidf == 'sublinear':
            self.tfidf = TfidfTransformer(smooth_idf=False, sublinear_tf=True)
        elif tfidf == 'smooth_sublinear':
            self.tfidf = TfidfTransformer(smooth_idf=True, sublinear_tf=True)

        # Select algorithm to use for neighbour computation
        algorithm = 'auto'
        self.method = NearestNeighbors(n_neighbors=10, algorithm=algorithm, metric='cosine')
        
        super(ContentBasedRecommender, self).__init__(items_path, train_path, test_path, val_path)
        
    def _process_item_features(self, items: DataFrame) -> DataFrame:
        """Processes the item metadata for feature generation

        Args:
            items (DataFrame): Dataframe containing items metadata

        Returns:
            DataFrame: Dataframe containing only relevant data for feature generation
        """
        columns = ["genres", "tags"]
        items = items.filter(columns)
        return items

    def _generate_item_features(self, items: DataFrame) -> DataFrame:
        """Generates feature vector of items and appends to returned DataFrame

        Args:
            items (DataFrame): dataframe containing the items

        Returns:
            DataFrame: dataframe with feature vector appended
        """
        items = self._process_item_features(items)
        # Combine all features into one column
        columns = items.columns.tolist()
        for col in columns:
            items[col] = items[col].fillna("").apply(set)
        items["tags"] = items.apply(lambda x: list(
            set.union(*([x[col] for col in columns]))), axis=1)
        columns.remove("tags")
        items = items.drop(columns, axis=1)

        # Compute one-hot encoded vector of tags
        mlb = MultiLabelBinarizer(sparse_output=self.sparse)
        if self.sparse:
            items = items.join(DataFrame.sparse.from_spmatrix(mlb.fit_transform(items.pop(
                "tags")), index=items.index, columns=["tag_" + c for c in mlb.classes_]))
        else:
            items = items.join(DataFrame(mlb.fit_transform(items.pop(
                "tags")), index=items.index, columns=["tag_" + c for c in mlb.classes_]))

        return items

    def generate_recommendations(self, amount=10, read_max=None) -> None:
        """Generate recommendations based on user review data

        Args:
            amount (int, optional): Amount of times to recommend. Defaults to 10.
            read_max (int, optional): Max amount of users to read. Defaults to None.
        """
        items = self.items
        df = self.train.iloc[:read_max].copy(deep=True) if read_max else self.train

        # Drop id so only feature vector is left
        if self.sparse:
            X = scipy.sparse.csr_matrix(items.values)
        else:
            X = np.array(items.values)

        if self.tfidf:
            # Use tf-idf
            X = self.tfidf.fit_transform(X)
            
        if self.normalize:
            X = self.normalizer.fit_transform(X)

        # Transformed feature vector back into items
        if self.sparse:
            items = DataFrame.sparse.from_spmatrix(X)
        else:
            items = DataFrame(X)

        self.method.set_params(n_neighbors=amount)
        nbrs = self.method.fit(X)

        recommendation_list = []
        for index, row in tqdm(df.iterrows()):
            # Compute uservector and recommendations for all users
            owned_items = items.iloc[row["item_id"],:]

            # If user has no items, no usable data is available
            assert not owned_items.empty

            # Computing average, assuming all user items are indication of interest
            user_vector = owned_items.mean()

            if self.normalize:
                user_vector = self.normalizer.transform([user_vector.to_numpy()])
            else:
                user_vector = [user_vector.to_numpy()]
            # Start overhead of 20%
            gen_am = amount//5
            recommendations = []
            while len(recommendations) < amount:
                # calculate amount of items to be generated
                gen_am += amount - len(recommendations)
                nns = nbrs.kneighbors(user_vector, gen_am, return_distance=True)
                # Filter out items in training set
                recommendations = list(filter(lambda id: id not in row["item_id"], nns[1][0]))

            recommendation_list.append(recommendations[:amount])

        df["recommendations"] = recommendation_list
        self.recommendations = df



class ImprovedRecommender(ContentBasedRecommender):
    def __init__(self, items_path: str, train_path: str, test_path: str, val_path: str, reviews_path: str, sparse: bool = True, dim_red=None, tfidf='default', use_feedback:bool=True, normalize:bool=False) -> None:
        """Improved content based recommender

        Args:
            items_path (str): Path to pickle file containing the items
            train_path (str): Path to train data parquet file
            test_path (str): Path to test data parquet file
            val_path (str): Path to validation data parquet file
            reviews_path (str): Path to reviews parquet file
            sparse (bool, optional): If sparse representation should be used. Defaults to True.
            dim_red (Object, optional): Which dimensionality reduction method to use. Defaults to None.
            tfidf (str, optional): Which tf-idf method to use. Defaults to 'default'.
            use_feedback(bool, optional): If feedback weighing should be used. Defaults to True.
            normalize (bool, optional): If normalization should be used. Defaults to False.
        """
        self.dim_red = dim_red
        self.use_feedback = use_feedback
        self.reviews = pd.read_parquet(reviews_path)

        super(ImprovedRecommender, self).__init__(items_path, train_path, test_path, val_path, sparse, tfidf, normalize)
        
    def _process_item_features(self, items):
        """Processes the item metadata for feature generation

        Args:
            items (DataFrame): Dataframe containing items metadata

        Returns:
            DataFrame: Dataframe containing only relevant data for feature generation
        """
        columns = ["genres", "tags", "specs", "developer", "publisher"]
        items = items.filter(columns)
        return items

    def generate_recommendations(self, amount=10, read_max=None) -> None:
        """Generate recommendations based on user review data

        Args:
            amount (int, optional): Amount of times to recommend. Defaults to 10.
            read_max (int, optional): Max amount of users to read. Defaults to None.
        """
        items = self.items
        
        df = self.train.iloc[:read_max].copy(deep=True) if read_max else self.train

        # Drop id so only feature vector is left
        if self.sparse:
            X = scipy.sparse.csr_matrix(items.values)
        else:
            X = np.array(items.values)

        if self.tfidf:
            # Use tf-idf
            X = self.tfidf.fit_transform(X)

        if self.dim_red:
            # Use dimensionality reduction
            X = self.dim_red.fit_transform(X)
            
        if self.normalize:
            X = self.normalizer.fit_transform(X)

        # Combine transformed feature vector back into items
        if self.sparse and self.dim_red:
            warnings.warn("Sparse was set to 'True' but dimensionality reduction is used, using dense matrix representation instead.", RuntimeWarning)
        if self.sparse and self.dim_red is None:
            items = DataFrame.sparse.from_spmatrix(X)
        else:
            items = DataFrame(X)

        self.method.set_params(n_neighbors=amount)
        nbrs = self.method.fit(X)

        recommendation_list = []
        for index, row in tqdm(df.iterrows()):
            # Compute uservector and recommendations for all users
            inventory_items = items.iloc[row["item_id"],:].copy(deep=True)

            # If user has no inventory items, no usable data is available
            assert not inventory_items.empty

            user_vector = None
            if self.use_feedback:
                playtime_weights = np.log2(np.array(row["playtime_forever"])+1)
                feedback = None
                inventory_items['weight'] = 0
                inventory_items['feedback'] = False
                inventory_items['sentiment'] = self.metadata.loc[row["item_id"], :]['sentiment']
                if index in self.reviews.index:
                    # use explicit feedback
                    feedback = self.reviews.loc[index,:]
                    for like, review in zip(feedback['recommend'], feedback['reviews']):
                        if review in inventory_items.index:
                            inventory_items.at[review, 'weight'] = 1 if like else 0
                            inventory_items.at[review, 'feedback'] = True
                inventory_items[inventory_items['feedback'] == False]['weight'] = inventory_items['sentiment']
                inventory_items['weight'] = np.where(inventory_items['feedback'] == False, inventory_items['sentiment'], inventory_items['weight'])
                print(inventory_items[['weight']].head(10))
                assert False
            else:
                # Computing average, assuming all inventory items are indication of interest
                user_vector = inventory_items.mean()

            if self.normalize:
                user_vector = self.normalizer.transform([user_vector.to_numpy()])
            else:
                user_vector = [user_vector.to_numpy()]
            # Start overhead of 20%
            gen_am = amount//5
            recommendations = []
            while len(recommendations) < amount:
                # calculate amount of items to be generated
                gen_am += amount - len(recommendations)
                nns = nbrs.kneighbors(user_vector, gen_am, return_distance=True)
                # Filter out items in reviews
                recommendations = list(filter(lambda id: id not in row["item_id"], nns[1][0]))

            recommendation_list.append(recommendations[:amount])

        df["recommendations"] = recommendation_list
        self.recommendations = df

class PopBasedRecommender(BaseRecommender):
    def __init__(self, train_path: str, test_path: str, val_path: str) -> None:
        """Popularity based recommender

        Args:
            train_path (str): Path to train data parquet file
            test_path (str): Path to test data parquet file
            val_path (str): Path to validation data parquet file
        """
        self.train = pd.read_parquet(train_path)
        self.test = pd.read_parquet(test_path)
        self.val = pd.read_parquet(val_path)

    def generate_recommendations(self, read_max=None) -> None:
        """Generates recommendations based on popularity of the items

        Args:
            read_max (int, optional): Max amount of users to read. Defaults to None.
        """
        df = self.train.iloc[:read_max].copy(deep=True) if read_max else self.train

        n_game_pop = df["item_id"].explode()
        n_game_pop.dropna(inplace=True)
        n_game_pop = n_game_pop.value_counts()

        df["recommendations"] = df["item_id"].apply(lambda x: [rec for rec in n_game_pop.index if rec not in x][:10]) 
        self.recommendations = df
